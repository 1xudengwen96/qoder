use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct ProxyConfig {
    pub port: u16,
    pub target_domains: Vec<String>,
    pub auto_rotate: bool,
    pub rotate_strategy: RotateStrategy,
    pub auto_start: bool,
    pub max_retry: u32,
    pub language: String,
    pub account_pool_url: String,
    #[serde(default = "default_balance_threshold")]
    pub balance_threshold: u64,
}

fn default_balance_threshold() -> u64 { 10 }

/// 默认拦截的 Qoder 计费节点。
///
/// 依据 IDE 1.106.3 的区域配置缓存（`region-config-cache.json`）：推理走 `api3.qoder.sh`，
/// 补全走 `api5.qoder.com.cn`，两者都计入 Credits，必须解密才能统计与轮换；
/// `center.qoder.sh` 是大模型节点，`openapi.qoder.sh` 提供计划与配额。
/// 代码库节点 `api2.qoder.sh` 不计费且请求体会带上整段代码，默认不解密。
pub const DEFAULT_TARGET_DOMAINS: [&str; 4] = [
    "center.qoder.sh",
    "openapi.qoder.sh",
    "api3.qoder.sh",
    "api5.qoder.com.cn",
];

pub fn default_target_domains() -> Vec<String> {
    DEFAULT_TARGET_DOMAINS.iter().map(|domain| (*domain).to_string()).collect()
}

/// IDE 区域配置缓存的默认位置；该文件由 IDE 每 1800 秒刷新一次。
pub fn region_cache_path() -> Option<PathBuf> {
    std::env::var_os("APPDATA").map(|base| {
        PathBuf::from(base)
            .join("Qoder")
            .join("User")
            .join("region-config-cache.json")
    })
}

/// 从区域配置缓存里取出计费节点（推理、补全）的主机名；读不到或格式不符时返回空表。
pub fn region_billing_domains(path: &Path) -> Vec<String> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) else {
        return Vec::new();
    };
    let Some(content) = value.get("content") else {
        return Vec::new();
    };
    ["preferredInferenceNode", "preferredNesNode"]
        .iter()
        .filter_map(|key| content.get(*key)?.get("endpoint")?.as_str())
        .filter_map(endpoint_host)
        .collect()
}

/// 只接受 https 端点里的主机名；其它协议、带端口或空值一律拒绝，不猜。
fn endpoint_host(endpoint: &str) -> Option<String> {
    let rest = endpoint.trim().strip_prefix("https://")?;
    let host = rest.split(['/', '?', '#']).next()?.rsplit('@').next()?;
    let host = host.split(':').next()?.to_ascii_lowercase();
    let valid = !host.is_empty()
        && host.chars().all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '-');
    valid.then_some(host)
}

fn is_default_scope(configured: &[String]) -> bool {
    configured.len() == DEFAULT_TARGET_DOMAINS.len()
        && configured.iter().all(|item| DEFAULT_TARGET_DOMAINS.contains(&item.as_str()))
}

/// 计算实际拦截的域名：产品默认 / 用户定制名单，再按需并入区域配置的计费节点。
///
/// 用户定制过名单时不做合并，避免悄悄加回他删掉的域名；名单为空或仍是产品默认时
/// 才并入，因为这种情况下区域配置漂移会直接导致代理看不到计费流量。
pub fn effective_target_domains(configured: &[String], region: &[String]) -> Vec<String> {
    let customized = !configured.is_empty() && !is_default_scope(configured);
    let mut domains = if configured.is_empty() {
        default_target_domains()
    } else {
        configured.to_vec()
    };
    if !customized {
        for host in region {
            if !domains.iter().any(|item| item == host) {
                domains.push(host.clone());
            }
        }
    }
    domains
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub enum RotateStrategy {
    Sequential,
    Random,
    Priority,
    LeastUsed,
    ByExpiry,
}

impl Default for ProxyConfig {
    fn default() -> Self {
        Self {
            port: 5888,
            target_domains: default_target_domains(),
            auto_rotate: true,
            rotate_strategy: RotateStrategy::Sequential,
            auto_start: false,
            max_retry: 3,
            language: "zh".to_string(),
            // 账号池必须由使用者自建、自行填写。这里不预设任何第三方地址，
            // 否则卡密会被发送到别人的服务器，且对方停服后本应用整体不可用。
            account_pool_url: String::new(),
            balance_threshold: 10,
        }
    }
}

impl ProxyConfig {
    pub fn load(path: &Path) -> Self {
        match std::fs::read_to_string(path) {
            Ok(data) => serde_json::from_str(&data).unwrap_or_default(),
            Err(_) => Self::default(),
        }
    }

    pub fn save(&self, path: &Path) -> Result<(), String> {
        let data = serde_json::to_string_pretty(self)
            .map_err(|e| format!("序列化配置失败: {}", e))?;
        std::fs::write(path, data)
            .map_err(|e| format!("写入配置失败: {}", e))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_cover_the_billing_nodes() {
        let config = ProxyConfig::default();
        for domain in ["center.qoder.sh", "openapi.qoder.sh", "api3.qoder.sh", "api5.qoder.com.cn"] {
            assert!(config.target_domains.iter().any(|item| item == domain), "缺少 {domain}");
        }
    }

    #[test]
    fn codebase_node_stays_out_of_the_default_scope() {
        let config = ProxyConfig::default();
        assert!(!config.target_domains.iter().any(|item| item == "api2.qoder.sh"));
    }

    #[test]
    fn empty_domain_list_falls_back_to_the_same_defaults() {
        assert_eq!(default_target_domains(), ProxyConfig::default().target_domains);
        assert!(!default_target_domains().is_empty());
    }

    #[test]
    fn region_nodes_extend_the_default_scope() {
        let region = vec!["api7.qoder.sh".to_string()];
        let mut expected = default_target_domains();
        expected.push("api7.qoder.sh".to_string());
        assert_eq!(effective_target_domains(&[], &region), expected);
        assert!(effective_target_domains(&default_target_domains(), &region).contains(&"api7.qoder.sh".to_string()));
    }

    #[test]
    fn customized_scope_is_left_untouched() {
        let configured = vec!["center.qoder.sh".to_string()];
        let region = vec!["api7.qoder.sh".to_string()];
        assert_eq!(effective_target_domains(&configured, &region), configured);
    }

    #[test]
    fn region_duplicates_are_not_added_twice() {
        let region = vec!["api3.qoder.sh".to_string(), "api3.qoder.sh".to_string()];
        let domains = effective_target_domains(&[], &region);
        assert_eq!(domains.iter().filter(|item| *item == "api3.qoder.sh").count(), 1);
    }

    #[test]
    fn only_billing_nodes_of_the_region_cache_are_read() {
        let path = std::env::temp_dir().join(format!("qoder-region-{}-billing.json", std::process::id()));
        let payload = r#"{"content":{"preferredInferenceNode":{"endpoint":"https://api9.qoder.sh/v1"},"preferredNesNode":{"endpoint":"http://plain.qoder.sh"},"preferredCodebaseNode":{"endpoint":"https://api2.qoder.sh"}}}"#;
        std::fs::write(&path, payload).unwrap();
        let hosts = region_billing_domains(&path);
        std::fs::remove_file(&path).ok();
        assert_eq!(hosts, vec!["api9.qoder.sh".to_string()]);
    }

    #[test]
    fn missing_or_broken_cache_yields_no_domains() {
        let path = std::env::temp_dir().join(format!("qoder-region-{}-absent.json", std::process::id()));
        assert!(region_billing_domains(&path).is_empty());
        std::fs::write(&path, "not json").unwrap();
        assert!(region_billing_domains(&path).is_empty());
        std::fs::remove_file(&path).ok();
    }
}
