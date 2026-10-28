use crate::auth::token::TokenManager;
use crate::config::{effective_target_domains, region_billing_domains, region_cache_path, ProxyConfig};
use crate::logger::{RequestLogger, TrafficLogger};
use crate::metrics::ProxyMetrics;
use crate::proxy::cert::CertManager;
use crate::proxy::server::ProxyServer;
use std::path::Path;
use std::sync::Arc;
use tokio::sync::RwLock;

/// 区域配置的刷新周期，与 IDE 自身的缓存刷新保持一致。
const REGION_REFRESH_SECONDS: u64 = 1800;

pub struct AppState {
    pub token_manager: Arc<TokenManager>,
    pub cert_manager: CertManager,
    pub proxy_server: Arc<RwLock<Option<ProxyServer>>>,
    pub proxy_port: u16,
    pub metrics: Arc<ProxyMetrics>,
    pub logger: Arc<RequestLogger>,
    pub traffic_logger: Arc<TrafficLogger>,
    pub config: Arc<RwLock<ProxyConfig>>,
    pub config_path: std::path::PathBuf,
}

impl AppState {
    pub fn new(data_dir: &std::path::Path, port: u16) -> Self {
        let token_path = data_dir.join("proxy-tokens.json");
        let token_manager = Arc::new(TokenManager::new(token_path));
        let cert_manager = CertManager::new(data_dir);

        let config_path = data_dir.join("proxy-config.json");
        let config = ProxyConfig::load(&config_path);

        // 拦截范围以区域配置为准：常量只是兜底，服务端每 1800 秒可改写推理与补全节点。
        let region = region_cache_path()
            .map(|path| region_billing_domains(&path))
            .unwrap_or_default();
        let target_domains = effective_target_domains(&config.target_domains, &region);

        let proxy_port = if config.port > 0 { config.port } else { port };

        let config = Arc::new(RwLock::new(config));
        let logger = Arc::new(RequestLogger::new(200, target_domains));
        // IDE 会在运行中改写区域缓存，启动后按同一周期重读，不让拦截名单停在旧节点。
        spawn_region_refresh(config.clone(), logger.clone());

        Self {
            token_manager,
            cert_manager,
            proxy_server: Arc::new(RwLock::new(None)),
            proxy_port,
            metrics: Arc::new(ProxyMetrics::new()),
            logger,
            traffic_logger: Arc::new(TrafficLogger::new(data_dir)),
            config,
            config_path,
        }
    }
}

/// 按一次区域缓存重算拦截名单；缓存读不到或没有计费节点时保持现状。
async fn refresh_from_cache(config: &RwLock<ProxyConfig>, logger: &RequestLogger, path: &Path) {
    let region = region_billing_domains(path);
    if region.is_empty() {
        return;
    }
    let configured = config.read().await.target_domains.clone();
    let domains = effective_target_domains(&configured, &region);
    if domains != logger.get_target_domains().await {
        logger.set_target_domains(domains).await;
    }
}

fn spawn_region_refresh(config: Arc<RwLock<ProxyConfig>>, logger: Arc<RequestLogger>) {
    let Some(path) = region_cache_path() else {
        return;
    };
    tauri::async_runtime::spawn(async move {
        let mut ticker = tokio::time::interval(std::time::Duration::from_secs(REGION_REFRESH_SECONDS));
        // 第一拍立即返回，启动时已经算过一次。
        ticker.tick().await;
        loop {
            ticker.tick().await;
            refresh_from_cache(&config, &logger, &path).await;
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::DEFAULT_TARGET_DOMAINS;

    fn write_cache(name: &str, inference: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!("qoder-state-{}-{}.json", std::process::id(), name));
        let payload = r#"{"content":{"preferredInferenceNode":{"endpoint":"__HOST__"}}}"#
            .replace("__HOST__", inference);
        std::fs::write(&path, payload).unwrap();
        path
    }

    #[tokio::test]
    async fn drifted_node_is_added_to_the_interception_scope() {
        let path = write_cache("drift", "https://api7.qoder.sh");
        let config = RwLock::new(ProxyConfig::default());
        let logger = RequestLogger::new(10, ProxyConfig::default().target_domains);
        refresh_from_cache(&config, &logger, &path).await;
        std::fs::remove_file(&path).ok();
        let domains = logger.get_target_domains().await;
        assert!(domains.iter().any(|item| item == "api7.qoder.sh"));
        assert!(domains.iter().any(|item| item == DEFAULT_TARGET_DOMAINS[0]));
    }

    #[tokio::test]
    async fn customized_list_survives_the_refresh() {
        let path = write_cache("custom", "https://api7.qoder.sh");
        let config = RwLock::new(ProxyConfig {
            target_domains: vec!["center.qoder.sh".to_string()],
            ..ProxyConfig::default()
        });
        let logger = RequestLogger::new(10, vec!["center.qoder.sh".to_string()]);
        refresh_from_cache(&config, &logger, &path).await;
        std::fs::remove_file(&path).ok();
        assert_eq!(logger.get_target_domains().await, vec!["center.qoder.sh".to_string()]);
    }

    #[tokio::test]
    async fn missing_cache_keeps_the_current_scope() {
        let path = std::env::temp_dir().join(format!("qoder-state-{}-absent.json", std::process::id()));
        std::fs::remove_file(&path).ok();
        let config = RwLock::new(ProxyConfig::default());
        let logger = RequestLogger::new(10, vec!["center.qoder.sh".to_string()]);
        refresh_from_cache(&config, &logger, &path).await;
        assert_eq!(logger.get_target_domains().await, vec!["center.qoder.sh".to_string()]);
    }
}
