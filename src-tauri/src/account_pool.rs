use serde::{Deserialize, Serialize};

#[derive(Deserialize)]
struct PoolResponse {
    success: bool,
    data: Option<PoolAccountData>,
}

#[derive(Deserialize, Clone)]
pub struct PoolAccountData {
    pub email: String,
    pub user_type: String,
    pub expire_date: String,
    pub user_info_json: String,
    pub user_plan_json: String,
    pub machine_token: String,
    pub machine_id: String,
    pub machine_code: String,
    pub machine_type: String,
}

#[derive(Deserialize)]
struct UserInfo {
    id: Option<String>,
    token: Option<String>,
    name: Option<String>,
    email: Option<String>,
    quota: Option<u64>,
}

#[derive(Serialize, Clone)]
pub struct FetchedAccount {
    pub user_id: String,
    pub token: String,
    pub name: String,
    pub email: String,
    pub user_type: String,
    pub expire_date: String,
    pub machine_token: String,
    pub machine_id: String,
    pub machine_code: String,
    pub machine_type: String,
    pub quota: Option<u64>,
}

/// 账号池地址未配置时的提示。使用方必须自己部署账号池，不允许回退到任何内置地址。
pub const POOL_URL_NOT_CONFIGURED: &str =
    "账号池地址未配置：请在设置中填写你自建的账号池接口，例如 https://pool.example.com/api/fetch-account";

/// 校验账号池地址。空值直接拒绝，避免把卡密发往未指定的主机。
pub fn validate_api_url(api_url: &str) -> Result<&str, String> {
    let trimmed = api_url.trim();
    if trimmed.is_empty() {
        return Err(POOL_URL_NOT_CONFIGURED.to_string());
    }
    if !(trimmed.starts_with("https://") || trimmed.starts_with("http://")) {
        return Err(format!("账号池地址必须以 http:// 或 https:// 开头：{trimmed}"));
    }
    Ok(trimmed)
}

/// 从账号池获取账号
pub async fn fetch_account(secret_key: &str, api_url: &str) -> Result<FetchedAccount, String> {
    let api_url = validate_api_url(api_url)?;
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .map_err(|e| format!("创建客户端失败: {}", e))?;

    let body = serde_json::json!({ "secret_key": secret_key });

    let resp = client
        .post(api_url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("请求账号池失败: {}", e))?;

    let pool_resp: PoolResponse = resp.json().await
        .map_err(|e| format!("解析响应失败: {}", e))?;

    if !pool_resp.success {
        return Err("账号池返回失败，密钥无效或已过期".to_string());
    }

    let data = pool_resp.data.ok_or("响应中无账号数据".to_string())?;

    // 解析 user_info_json
    let user_info: UserInfo = serde_json::from_str(&data.user_info_json)
        .map_err(|e| format!("解析用户信息失败: {}", e))?;

    let token = user_info.token.ok_or_else(|| "账号池返回数据缺少 token".to_string())?;
    let user_id = user_info.id.unwrap_or_default();

    Ok(FetchedAccount {
        user_id,
        token,
        name: user_info.name.unwrap_or_default(),
        email: user_info.email.unwrap_or(data.email.clone()),
        user_type: data.user_type,
        expire_date: data.expire_date,
        machine_token: data.machine_token,
        machine_id: data.machine_id,
        machine_code: data.machine_code,
        machine_type: data.machine_type,
        quota: user_info.quota,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blank_pool_url_is_rejected() {
        for blank in ["", "   ", "\t"] {
            let error = validate_api_url(blank).unwrap_err();
            assert!(error.contains("未配置"), "{blank:?} -> {error}");
        }
    }

    #[test]
    fn pool_url_without_scheme_is_rejected() {
        for bad in ["pool.example.com/api/fetch-account", "ftp://pool.example.com", "pool.example.com"] {
            let error = validate_api_url(bad).unwrap_err();
            assert!(error.contains("http://"), "{bad} -> {error}");
        }
    }

    #[test]
    fn pool_url_is_trimmed_and_accepted() {
        assert_eq!(
            validate_api_url("  https://pool.example.com/api/fetch-account  ").unwrap(),
            "https://pool.example.com/api/fetch-account"
        );
        assert_eq!(
            validate_api_url("http://127.0.0.1:9000/api").unwrap(),
            "http://127.0.0.1:9000/api"
        );
    }

    #[test]
    fn default_config_does_not_point_at_a_third_party_pool() {
        let config = crate::config::ProxyConfig::default();
        assert!(config.account_pool_url.is_empty());
    }
}
