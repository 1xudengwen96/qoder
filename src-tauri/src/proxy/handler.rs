use crate::auth::cosy::build_cosy_headers;
use crate::auth::token::TokenManager;
use crate::config::ProxyConfig;
use crate::logger::{
    format_headers, is_websocket_upgrade, RequestLogEntry, RequestLogger, TrafficLogger,
};
use crate::metrics::ProxyMetrics;
use http_body_util::BodyExt;
use hudsucker::{Body, HttpContext, HttpHandler, RequestOrResponse};
use hyper::{Method, Request, Response, StatusCode};
use std::sync::Arc;
use tokio::sync::RwLock;

#[derive(Clone)]
pub struct QoderProxyHandler {
    pub token_manager: Arc<TokenManager>,
    pub auto_rotate: Arc<RwLock<bool>>,
    pub metrics: Arc<ProxyMetrics>,
    pub logger: Arc<RequestLogger>,
    pub traffic_logger: Arc<TrafficLogger>,
    pub config: Arc<RwLock<ProxyConfig>>,
    // Hudsucker gives each request/response pair its own handler instance.
    // These values must be copied on clone, not shared across concurrent pairs.
    pub last_log_id: Option<u64>,
    pub last_request_is_target: bool,
}

impl QoderProxyHandler {
    async fn intercepts(&self, req: &Request<Body>) -> bool {
        self.logger
            .is_target_domain(req.uri().host().unwrap_or(""))
            .await
    }

    async fn process_request(&mut self, req: Request<Body>) -> RequestOrResponse {
        let host = req.uri().host().unwrap_or("").to_string();
        let path = req.uri().path().to_string();
        let method = req.method().to_string();
        let is_target = req.method() != Method::CONNECT && self.intercepts(&req).await;
        self.last_log_id = None;
        self.last_request_is_target = is_target;
        self.logger.record_domain(host.clone()).await;
        let req_headers_str = format_headers(req.headers());
        let is_ws = is_websocket_upgrade(req.headers());
        let mut current_label = None;
        let mut injected = false;

        let result = if is_target {
            // Legacy local-account routing. The cloud client will replace this
            // with platform authorization; this is not the cloud billing path.
            if *self.auto_rotate.read().await {
                let threshold = self.config.read().await.balance_threshold;
                if threshold > 0 {
                    if let Some(current) = self.token_manager.get_current().await {
                        let remaining = current
                            .quota_total
                            .unwrap_or(u64::MAX)
                            .saturating_sub(current.quota_used.unwrap_or(0));
                        if remaining < threshold {
                            if self
                                .token_manager
                                .rotate_to_sufficient_balance(threshold)
                                .await
                                .is_some()
                            {
                                self.metrics.record_rotation();
                            }
                        }
                    }
                }
            }
            if let Some(account) = self.token_manager.get_current().await {
                let (parts, body) = req.into_parts();
                let body_bytes = match body.collect().await {
                    Ok(collected) => collected.to_bytes(),
                    Err(_) => return RequestOrResponse::Response(local_failure()),
                };
                let body_str = String::from_utf8_lossy(&body_bytes);
                let headers = match build_cosy_headers(
                    &account.token,
                    &account.user_id,
                    &account.name,
                    &account.email,
                    &path,
                    &body_str,
                ) {
                    Ok(headers) => headers,
                    Err(_) => return RequestOrResponse::Response(local_failure()),
                };
                let values = [
                    ("authorization", headers.authorization),
                    ("cosy-user", headers.cosy_user),
                    ("cosy-key", headers.cosy_key),
                    ("cosy-date", headers.cosy_date),
                    ("x-request-id", headers.request_id),
                ];
                let mut parsed = Vec::with_capacity(values.len());
                for (name, value) in values {
                    match value.parse::<hyper::header::HeaderValue>() {
                        Ok(value) => parsed.push((name, value)),
                        Err(_) => return RequestOrResponse::Response(local_failure()),
                    }
                }
                self.metrics.record_request(true, body_bytes.len() as u64);
                current_label = Some(account.label);
                injected = true;
                let mut req = Request::from_parts(parts, Body::from(body_bytes));
                for (name, value) in parsed {
                    req.headers_mut().insert(name, value);
                }
                req
            } else {
                self.metrics.record_request(false, 0);
                req
            }
        } else {
            // Leave unrelated requests and CONNECT bodies unconsumed.
            self.metrics.record_request(false, 0);
            req
        };

        let mut entry = RequestLogEntry {
            id: 0,
            timestamp: chrono::Utc::now().to_rfc3339(),
            method,
            host,
            path,
            status: None,
            injected,
            token_label: current_label,
            request_headers: Some(req_headers_str),
            request_body: None,
            response_headers: None,
            response_body: None,
            is_websocket: is_ws,
        };
        let entry_id = self.logger.log_request(entry.clone()).await;
        entry.id = entry_id;
        self.traffic_logger.log_traffic(&entry);
        self.last_log_id = Some(entry_id);
        RequestOrResponse::Request(result)
    }

    async fn process_response(&mut self, resp: Response<Body>) -> Response<Body> {
        let status = resp.status().as_u16();
        // Unrelated websites must never rotate the user's Qoder account pool.
        if self.last_request_is_target
            && (status == 401 || status == 403)
            && *self.auto_rotate.read().await
        {
            self.metrics.record_auth_failure();
            let strategy = self.config.read().await.rotate_strategy.clone();
            if self
                .token_manager
                .rotate_with_strategy(strategy)
                .await
                .is_some()
            {
                self.metrics.record_rotation();
            }
        }
        if let Some(entry_id) = self.last_log_id {
            let headers = format_headers(resp.headers());
            let note = "(response streamed; body not captured)";
            self.logger
                .update_response(entry_id, status, headers.clone(), note.to_string())
                .await;
            self.traffic_logger
                .log_response(entry_id, status, &headers, note);
        }
        // Preserve streaming, cancellation, errors and trailers. Never collect
        // the response merely to log it: SSE can remain open indefinitely.
        resp
    }
}

fn local_failure() -> Response<Body> {
    let mut response = Response::new(Body::from("Local proxy could not prepare this request."));
    *response.status_mut() = StatusCode::BAD_GATEWAY;
    response
}

impl HttpHandler for QoderProxyHandler {
    async fn handle_request(
        &mut self,
        _ctx: &HttpContext,
        req: Request<Body>,
    ) -> RequestOrResponse {
        self.process_request(req).await
    }

    async fn handle_response(
        &mut self,
        _ctx: &HttpContext,
        resp: Response<Body>,
    ) -> Response<Body> {
        self.process_response(resp).await
    }

    async fn should_intercept(&mut self, _ctx: &HttpContext, req: &Request<Body>) -> bool {
        self.intercepts(req).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::AppState;
    use hyper::body::{Body as HttpBody, Bytes, Frame};
    use std::collections::VecDeque;
    use std::pin::Pin;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::task::{Context, Poll};
    use std::time::Duration;

    struct TestData(std::path::PathBuf);
    impl Drop for TestData {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn handler() -> (TestData, QoderProxyHandler) {
        let data = TestData(
            std::env::temp_dir().join(format!("qoder-proxy-test-{}", uuid::Uuid::new_v4())),
        );
        let state = AppState::new(&data.0, 5888);
        let handler = QoderProxyHandler {
            token_manager: state.token_manager,
            auto_rotate: Arc::new(RwLock::new(true)),
            metrics: state.metrics,
            logger: state.logger,
            traffic_logger: state.traffic_logger,
            config: state.config,
            last_log_id: None,
            last_request_is_target: false,
        };
        (data, handler)
    }

    struct TestBody {
        polls: Arc<AtomicUsize>,
        frames: VecDeque<Frame<Bytes>>,
        pending: bool,
    }
    impl HttpBody for TestBody {
        type Data = Bytes;
        type Error = hudsucker::Error;
        fn poll_frame(
            mut self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            self.polls.fetch_add(1, Ordering::Relaxed);
            if let Some(frame) = self.frames.pop_front() {
                Poll::Ready(Some(Ok(frame)))
            } else if self.pending {
                Poll::Pending
            } else {
                Poll::Ready(None)
            }
        }
    }

    #[tokio::test]
    async fn indefinitely_open_response_is_returned_without_polling_body() {
        let (_data, mut handler) = handler();
        handler.last_request_is_target = true;
        let polls = Arc::new(AtomicUsize::new(0));
        let response = Response::new(Body::from(
            TestBody {
                polls: polls.clone(),
                frames: VecDeque::new(),
                pending: true,
            }
            .boxed(),
        ));
        let result = tokio::time::timeout(
            Duration::from_millis(100),
            handler.process_response(response),
        )
        .await;
        assert!(result.is_ok());
        assert_eq!(polls.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn response_data_headers_and_trailers_are_preserved() {
        let (_data, mut handler) = handler();
        handler.last_request_is_target = true;
        let mut trailers = hyper::HeaderMap::new();
        trailers.insert("x-stream-status", "complete".parse().unwrap());
        let mut response = Response::new(Body::from(
            TestBody {
                polls: Arc::new(AtomicUsize::new(0)),
                frames: VecDeque::from([
                    Frame::data(Bytes::from_static(b"data: first\n\n")),
                    Frame::trailers(trailers),
                ]),
                pending: false,
            }
            .boxed(),
        ));
        response
            .headers_mut()
            .insert("content-type", "text/event-stream".parse().unwrap());
        let mut result = handler.process_response(response).await;
        assert_eq!(result.headers()["content-type"], "text/event-stream");
        assert_eq!(
            result
                .body_mut()
                .frame()
                .await
                .unwrap()
                .unwrap()
                .into_data()
                .unwrap(),
            "data: first\n\n"
        );
        let trailers = result
            .body_mut()
            .frame()
            .await
            .unwrap()
            .unwrap()
            .into_trailers()
            .unwrap();
        assert_eq!(trailers["x-stream-status"], "complete");
    }

    #[tokio::test]
    async fn concurrent_pairs_update_their_own_log_records() {
        let (_data, first) = handler();
        let logger = first.logger.clone();
        let mut first = first;
        let mut second = first.clone();
        let request = |path: &str| {
            Request::builder()
                .uri(format!("https://example.test/{}", path))
                .body(Body::empty())
                .unwrap()
        };
        first.process_request(request("first")).await;
        second.process_request(request("second")).await;
        assert_ne!(first.last_log_id, second.last_log_id);
        let mut rejected = Response::new(Body::empty());
        *rejected.status_mut() = StatusCode::FORBIDDEN;
        let _ = tokio::join!(
            first.process_response(Response::new(Body::empty())),
            second.process_response(rejected)
        );
        let logs = logger.get_logs(10).await;
        assert_eq!(
            logs.iter().find(|row| row.path == "/first").unwrap().status,
            Some(200)
        );
        assert_eq!(
            logs.iter()
                .find(|row| row.path == "/second")
                .unwrap()
                .status,
            Some(403)
        );
        assert_eq!(first.metrics.snapshot().auth_failures, 0);
    }

    #[tokio::test]
    async fn unrelated_request_body_is_not_buffered_or_logged() {
        let (_data, mut handler) = handler();
        let polls = Arc::new(AtomicUsize::new(0));
        let request = Request::builder()
            .uri("https://example.test/private")
            .header("authorization", "private-sentinel")
            .body(Body::from(
                TestBody {
                    polls: polls.clone(),
                    frames: VecDeque::new(),
                    pending: true,
                }
                .boxed(),
            ))
            .unwrap();
        let result =
            tokio::time::timeout(Duration::from_millis(100), handler.process_request(request))
                .await;
        assert!(matches!(result, Ok(RequestOrResponse::Request(_))));
        assert_eq!(polls.load(Ordering::Relaxed), 0);
        let logs = handler.logger.get_logs(1).await;
        assert!(logs[0].request_body.is_none());
        assert!(!logs[0]
            .request_headers
            .as_ref()
            .unwrap()
            .contains("private-sentinel"));
    }

    struct ErrorBody;
    impl HttpBody for ErrorBody {
        type Data = Bytes;
        type Error = hudsucker::Error;
        fn poll_frame(
            self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
        ) -> Poll<Option<Result<Frame<Bytes>, Self::Error>>> {
            Poll::Ready(Some(Err(hudsucker::Error::Decode)))
        }
    }

    #[tokio::test]
    async fn failed_request_body_is_not_forwarded_as_empty_content() {
        let (_data, mut handler) = handler();
        handler
            .token_manager
            .add(
                "synthetic-token".to_string(),
                "test".to_string(),
                "test-user".to_string(),
                String::new(),
                String::new(),
                String::new(),
                String::new(),
            )
            .await;
        let request = Request::builder()
            .uri("https://openapi.qoder.sh/api")
            .body(Body::from(ErrorBody.boxed()))
            .unwrap();
        match handler.process_request(request).await {
            RequestOrResponse::Response(response) => {
                assert_eq!(response.status(), StatusCode::BAD_GATEWAY)
            }
            RequestOrResponse::Request(_) => panic!("failed body must not be forwarded"),
        }
    }

    #[tokio::test]
    async fn only_configured_domains_are_intercepted() {
        let (_data, handler) = handler();
        let request = |uri| Request::builder().uri(uri).body(Body::empty()).unwrap();
        assert!(
            handler
                .intercepts(&request("https://openapi.qoder.sh/api"))
                .await
        );
        let connect = Request::builder()
            .method(Method::CONNECT)
            .uri("openapi.qoder.sh:443")
            .body(Body::empty())
            .unwrap();
        assert!(handler.intercepts(&connect).await);
        assert!(
            !handler
                .intercepts(&request("https://example.test/private"))
                .await
        );
        assert!(
            !handler
                .intercepts(&request("https://openapi.qoder.sh.attacker.test/"))
                .await
        );
    }
}
