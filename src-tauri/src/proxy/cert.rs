use base64::Engine;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub struct CertManager {
    ca_dir: PathBuf,
    ca_init: Mutex<()>,
}

impl CertManager {
    pub fn new(data_dir: &Path) -> Self {
        let ca_dir = data_dir.join("proxy-ca");
        let _ = fs::create_dir_all(&ca_dir);
        Self {
            ca_dir,
            ca_init: Mutex::new(()),
        }
    }

    pub fn ca_key_path(&self) -> PathBuf {
        self.ca_dir.join("ca.key")
    }

    pub fn ca_cert_path(&self) -> PathBuf {
        self.ca_dir.join("ca.crt")
    }

    /// Generate the local CA before installation or proxy startup. Never replace
    /// an existing private key merely because the user starts the app again.
    pub fn ensure_ca(&self) -> Result<(), String> {
        use rcgen::{
            BasicConstraints, CertificateParams, DistinguishedName, DnType, IsCa, KeyPair,
        };

        let _guard = self
            .ca_init
            .lock()
            .map_err(|_| "证书初始化锁不可用".to_string())?;
        let key_path = self.ca_key_path();
        let cert_path = self.ca_cert_path();
        match (key_path.exists(), cert_path.exists()) {
            (true, true) => return Ok(()),
            (true, false) | (false, true) => {
                return Err(
                    "CA 证书和私钥不完整，请先备份并修复证书，不能自动覆盖现有身份".to_string(),
                );
            }
            (false, false) => {}
        }

        fs::create_dir_all(&self.ca_dir).map_err(|e| format!("创建证书目录失败: {}", e))?;
        let key_pair = KeyPair::generate().map_err(|e| format!("生成密钥对失败: {}", e))?;
        let mut name = DistinguishedName::new();
        name.push(DnType::CommonName, "Qoder Proxy CA");
        name.push(DnType::OrganizationName, "Qoder");
        let mut params = CertificateParams::default();
        params.distinguished_name = name;
        params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        let cert = params
            .self_signed(&key_pair)
            .map_err(|e| format!("自签名失败: {}", e))?;

        let mut options = fs::OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut key_file = options
            .open(&key_path)
            .map_err(|e| format!("创建 CA 私钥失败: {}", e))?;
        key_file
            .write_all(key_pair.serialize_pem().as_bytes())
            .map_err(|e| format!("写入 CA 私钥失败: {}", e))?;
        fs::write(&cert_path, cert.pem()).map_err(|e| format!("写入 CA 证书失败: {}", e))?;
        Ok(())
    }

    pub fn install_ca(&self) -> Result<(), String> {
        self.ensure_ca()?;
        let cert_path = self.ca_cert_path();
        // 读取 PEM 证书并转换为 DER
        let pem_data =
            fs::read_to_string(&cert_path).map_err(|e| format!("读取证书文件失败: {}", e))?;
        let der = pem_to_der(&pem_data)?;

        #[cfg(target_os = "windows")]
        {
            win_cert::install_cert_silent(&der)
        }
        #[cfg(not(target_os = "windows"))]
        {
            let _ = der;
            Err("当前平台不支持自动安装证书".to_string())
        }
    }

    pub fn uninstall_ca(&self) -> Result<(), String> {
        #[cfg(target_os = "windows")]
        {
            win_cert::uninstall_cert_silent("Qoder Proxy CA")
        }
        #[cfg(not(target_os = "windows"))]
        {
            Err("当前平台不支持自动卸载证书".to_string())
        }
    }

    pub fn is_installed(&self) -> bool {
        #[cfg(target_os = "windows")]
        {
            win_cert::is_cert_installed("Qoder Proxy CA")
        }
        #[cfg(not(target_os = "windows"))]
        {
            false
        }
    }
}

/// PEM 格式转 DER 二进制
fn pem_to_der(pem: &str) -> Result<Vec<u8>, String> {
    let pem = pem.trim();
    let b64: String = pem
        .lines()
        .filter(|l| !l.starts_with("-----"))
        .collect::<Vec<_>>()
        .join("");
    base64::engine::general_purpose::STANDARD
        .decode(&b64)
        .map_err(|e| format!("Base64 解码失败: {}", e))
}

#[cfg(target_os = "windows")]
mod win_cert {
    use windows::Win32::Security::Cryptography::*;

    /// 静默安装 CA 证书到当前用户的 Root 证书库
    pub fn install_cert_silent(cert_der: &[u8]) -> Result<(), String> {
        unsafe {
            let store_name = windows::core::w!("Root");
            let store = CertOpenStore(
                CERT_STORE_PROV_SYSTEM_W,
                CERT_QUERY_ENCODING_TYPE(0),
                None,
                CERT_OPEN_STORE_FLAGS(CERT_SYSTEM_STORE_CURRENT_USER),
                Some(store_name.as_ptr() as *const _),
            )
            .map_err(|e| format!("打开证书库失败: {}", e))?;

            // 创建证书上下文
            let cert_ctx =
                CertCreateCertificateContext(X509_ASN_ENCODING | PKCS_7_ASN_ENCODING, cert_der);
            if cert_ctx.is_null() {
                let _ = CertCloseStore(store, 0);
                return Err("创建证书上下文失败".to_string());
            }

            // 添加到证书库（如果已存在则替换）
            let result = CertAddCertificateContextToStore(
                store,
                cert_ctx,
                CERT_STORE_ADD_REPLACE_EXISTING,
                None,
            );

            let _ = CertFreeCertificateContext(Some(cert_ctx));
            let _ = CertCloseStore(store, 0);

            result.map_err(|e| format!("添加证书到证书库失败: {}", e))
        }
    }

    /// 静默卸载 CA 证书
    pub fn uninstall_cert_silent(subject_name: &str) -> Result<(), String> {
        unsafe {
            let store_name = windows::core::w!("Root");
            let store = CertOpenStore(
                CERT_STORE_PROV_SYSTEM_W,
                CERT_QUERY_ENCODING_TYPE(0),
                None,
                CERT_OPEN_STORE_FLAGS(CERT_SYSTEM_STORE_CURRENT_USER),
                Some(store_name.as_ptr() as *const _),
            )
            .map_err(|e| format!("打开证书库失败: {}", e))?;

            // 遍历查找匹配的证书并删除
            let mut cert_ctx = CertEnumCertificatesInStore(store, None);
            while !cert_ctx.is_null() {
                let mut name_buf = vec![0u8; 256];
                let name_len = CertGetNameStringA(
                    cert_ctx,
                    CERT_NAME_SIMPLE_DISPLAY_TYPE,
                    0,
                    None,
                    Some(&mut name_buf),
                );
                if name_len > 0 {
                    let name = String::from_utf8_lossy(&name_buf[..name_len as usize - 1]);
                    if name.contains(subject_name) {
                        // 复制上下文后删除（CertDeleteCertificateFromStore 会释放传入的 context）
                        let dup = CertDuplicateCertificateContext(Some(cert_ctx as *const _));
                        let _ = CertDeleteCertificateFromStore(dup);
                    }
                }
                cert_ctx = CertEnumCertificatesInStore(store, Some(cert_ctx));
            }

            let _ = CertCloseStore(store, 0);
            Ok(())
        }
    }

    /// 检测证书是否已安装
    pub fn is_cert_installed(subject_name: &str) -> bool {
        unsafe {
            let store_name = windows::core::w!("Root");
            let store = match CertOpenStore(
                CERT_STORE_PROV_SYSTEM_W,
                CERT_QUERY_ENCODING_TYPE(0),
                None,
                CERT_OPEN_STORE_FLAGS(CERT_SYSTEM_STORE_CURRENT_USER),
                Some(store_name.as_ptr() as *const _),
            ) {
                Ok(s) => s,
                Err(_) => return false,
            };

            let mut found = false;
            let mut cert_ctx = CertEnumCertificatesInStore(store, None);
            while !cert_ctx.is_null() {
                let mut name_buf = vec![0u8; 256];
                let name_len = CertGetNameStringA(
                    cert_ctx,
                    CERT_NAME_SIMPLE_DISPLAY_TYPE,
                    0,
                    None,
                    Some(&mut name_buf),
                );
                if name_len > 0 {
                    let name = String::from_utf8_lossy(&name_buf[..name_len as usize - 1]);
                    if name.contains(subject_name) {
                        found = true;
                        let _ = CertFreeCertificateContext(Some(cert_ctx));
                        break;
                    }
                }
                cert_ctx = CertEnumCertificatesInStore(store, Some(cert_ctx));
            }

            let _ = CertCloseStore(store, 0);
            found
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct TestData(PathBuf);

    impl TestData {
        fn new() -> Self {
            Self(std::env::temp_dir().join(format!("qoder-cert-test-{}", uuid::Uuid::new_v4())))
        }
    }

    impl Drop for TestData {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn first_run_generates_ca_without_starting_proxy() {
        let data = TestData::new();
        let manager = CertManager::new(&data.0);
        manager.ensure_ca().unwrap();
        let cert = fs::read_to_string(manager.ca_cert_path()).unwrap();
        assert!(cert.contains("BEGIN CERTIFICATE"));
        let key = fs::read_to_string(manager.ca_key_path()).unwrap();
        rcgen::KeyPair::from_pem(&key).unwrap();
    }

    #[test]
    fn repeated_initialization_preserves_existing_identity() {
        let data = TestData::new();
        let manager = CertManager::new(&data.0);
        manager.ensure_ca().unwrap();
        let before_key = fs::read(manager.ca_key_path()).unwrap();
        let before_cert = fs::read(manager.ca_cert_path()).unwrap();
        manager.ensure_ca().unwrap();
        assert_eq!(before_key, fs::read(manager.ca_key_path()).unwrap());
        assert_eq!(before_cert, fs::read(manager.ca_cert_path()).unwrap());
    }

    #[test]
    fn concurrent_initialization_uses_one_identity() {
        let data = TestData::new();
        let manager = std::sync::Arc::new(CertManager::new(&data.0));
        let workers: Vec<_> = (0..4)
            .map(|_| {
                let manager = manager.clone();
                std::thread::spawn(move || {
                    manager.ensure_ca().unwrap();
                    fs::read(manager.ca_cert_path()).unwrap()
                })
            })
            .collect();
        let certificates: Vec<_> = workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .collect();
        assert!(certificates.iter().all(|cert| cert == &certificates[0]));
    }

    #[test]
    fn incomplete_identity_is_not_silently_overwritten() {
        let data = TestData::new();
        let manager = CertManager::new(&data.0);
        fs::write(manager.ca_key_path(), "existing-key").unwrap();
        assert!(manager.ensure_ca().is_err());
        assert_eq!(
            fs::read_to_string(manager.ca_key_path()).unwrap(),
            "existing-key"
        );
        assert!(!manager.ca_cert_path().exists());
    }
}
