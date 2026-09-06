#!/bin/bash
set -e

# N.E.K.O. Docker Entrypoint Script with Nginx Reverse Proxy
PIDS=()
RELOADER_PID=""

# 设置环境变量
export NEKO_MAIN_SERVER_PORT=${NEKO_MAIN_SERVER_PORT:-48911}
export NGINX_PORT=${NGINX_PORT:-80}
export NGINX_SSL_PORT=${NGINX_SSL_PORT:-443}
export SSL_DOMAIN=${SSL_DOMAIN:-project-neko.online}
export SSL_DAYS=${SSL_DAYS:-365000}  # 1000年
export NGINX_AUTO_RELOAD=${NGINX_AUTO_RELOAD:-1}  # 是否启用自动重载，默认启用

# Docker 容器内 Nginx 作为反向代理前置，强制启用 uvicorn proxy_headers
export NEKO_BEHIND_PROXY=true
# Nginx 保留客户端 Host；将配置的部署域名显式加入应用层可信主机。
# loopback 和 IP 字面量由中间件默认允许，因此本地/IP 访问不依赖此项。
export NEKO_TRUSTED_HOSTS="${NEKO_TRUSTED_HOSTS:-${SSL_DOMAIN}}"

# 1. 信号处理优化
setup_signal_handlers() {
    trap 'echo "🛑 Received shutdown signal"; nginx -s stop 2>/dev/null || true; for pid in "${PIDS[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done; [ -n "$RELOADER_PID" ] && kill -TERM "$RELOADER_PID" 2>/dev/null || true; wait; exit 0' TERM INT
}

# 2. 环境检查与初始化优化
check_dependencies() {
    echo "🔍 Checking system dependencies..."
    
    # 确保完整的PATH设置
    export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/root/.local/bin:/root/.cargo/bin:$PATH"
    
    # 检查Python可用性
    if ! command -v python &> /dev/null; then
        echo "⚠️ Python3 not found. Installing python3.11..."
        apt-get update && apt-get install -y --no-install-recommends python3.11
    fi
    
    # 检查uv可用性
    if ! command -v uv &> /dev/null; then
        echo "⚠️ uv not found. Installing uv via official script..."
        
        # 使用官方安装脚本并指定安装位置
        wget -LsSf https://astral.sh/uv/install.sh | sh -s -- --install-dir /usr/local/bin
        
        # 确保安装目录在PATH中
        export PATH="/usr/local/bin:$PATH"
        
        # 验证安装
        if ! command -v uv &> /dev/null; then
            echo "❌ Failed to install uv. Attempting manual installation..."
            exit 1
        fi
    fi
    
    # 检查Nginx可用性
    if ! command -v nginx &> /dev/null; then
        echo "⚠️ Nginx not found. Installing nginx..."
        apt-get update && apt-get install -y --no-install-recommends nginx
    fi
    
    # 检查openssl可用性（用于证书验证和生成）
    if ! command -v openssl &> /dev/null; then
        echo "⚠️ OpenSSL not found. Installing openssl..."
        apt-get update && apt-get install -y --no-install-recommends openssl bc
    fi
    
    echo "✅ Dependencies checked:"
    echo "   UV version: $(uv --version 2>/dev/null || echo "Not found")"
    echo "   Python version: $(python3 --version 2>/dev/null || echo "Not found")"
    echo "   Nginx version: $(nginx -v 2>&1 | head -1 || echo "Not found")"
    echo "   OpenSSL version: $(openssl version 2>/dev/null || echo "Not found")"
}

# Configure the process and system timezone before any service or certificate
# code runs.  ``tzdata`` is installed in both image variants; an invalid
# user-provided TZ intentionally falls back to the image default rather than
# preventing the application from starting.
configure_timezone() {
    local timezone="${TZ:-Asia/Shanghai}"
    local zoneinfo="/usr/share/zoneinfo/$timezone"

    if [ ! -f "$zoneinfo" ]; then
        echo "⚠️ Invalid TZ '$timezone'; falling back to Asia/Shanghai"
        timezone="Asia/Shanghai"
        zoneinfo="/usr/share/zoneinfo/$timezone"
    fi

    ln -snf "$zoneinfo" /etc/localtime
    printf '%s\n' "$timezone" > /etc/timezone
    export TZ="$timezone"
    echo "🕒 Container timezone: $timezone"
}

# 输出详细的SSL证书信息（这个就是为了图一乐，不给关！）
print_certificate_fun_info() {
    local cert_file="$1"
    
    echo ""
    echo "🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉"
    echo "🎉   SSL证书详细报告 - 就是为了图一乐！  🎉"
    echo "🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉"
    echo ""
    
    if [ ! -f "$cert_file" ]; then
        echo "❌ 证书文件不存在，无法显示详细信息"
        return
    fi
    
    # 检查证书是否有效
    if ! openssl x509 -in "$cert_file" -noout 2>/dev/null; then
        echo "❌ 无效的证书格式"
        return
    fi
    
    # 获取证书详细信息
    echo "📄 证书基本信息:"
    echo "   🔸 证书文件: $cert_file"
    echo "   🔸 文件大小: $(ls -lh "$cert_file" | awk '{print $5}')"
    echo ""
    
    echo "👤 证书主题信息:"
    openssl x509 -in "$cert_file" -noout -subject 2>/dev/null | \
        sed 's/subject=/\n     📌 /' | tr ',' '\n' | sed 's/^/     🔹 /'
    echo ""
    
    echo "🏢 证书颁发者:"
    openssl x509 -in "$cert_file" -noout -issuer 2>/dev/null | \
        sed 's/issuer=/\n     📌 /' | tr ',' '\n' | sed 's/^/     🔹 /'
    echo ""
    
    echo "📅 证书有效期:"
    local not_before=$(openssl x509 -in "$cert_file" -noout -startdate 2>/dev/null | cut -d= -f2)
    local not_after=$(openssl x509 -in "$cert_file" -noout -enddate 2>/dev/null | cut -d= -f2)
    
    echo "   🔸 生效时间: $not_before"
    echo "   🔸 过期时间: $not_after"
    
    # 计算剩余天数
    local now_seconds=$(date +%s)
    local expire_seconds=$(date -d "$not_after" +%s 2>/dev/null || date -j -f "%b %d %T %Y" "$not_after" +%s 2>/dev/null)
    
    if [ -n "$expire_seconds" ]; then
        local days_left=$(( (expire_seconds - now_seconds) / 86400 ))
        local years_left=$(echo "scale=2; $days_left / 365" | bc)
        
        echo "   🔸 剩余天数: $days_left 天"
        echo "   🔸 剩余年数: $years_left 年"
        
        if [ $days_left -gt 365000 ]; then
            echo "   🎉 哇！这个证书能用 $(echo "scale=0; $days_left/365" | bc) 年以上！"
            echo "   🚀 这是要传给孙子用的节奏啊！"
        elif [ $days_left -gt 36500 ]; then
            echo "   👍 能用 $(echo "scale=0; $days_left/365" | bc) 年以上，不错！"
        elif [ $days_left -gt 3650 ]; then
            echo "   👌 能用 $(echo "scale=0; $days_left/365" | bc) 年，够用了！"
        elif [ $days_left -gt 365 ]; then
            echo "   ⏳ 还有 $(echo "scale=0; $days_left/365" | bc) 年多，不用急！"
        elif [ $days_left -gt 30 ]; then
            echo "   ⚠️ 只剩 $days_left 天了，注意续期！"
        elif [ $days_left -gt 0 ]; then
            echo "   🔴 只剩 $days_left 天了，赶紧续期！"
        else
            echo "   💀 证书已过期！"
        fi
    fi
    echo ""
    
    echo "🌍 证书包含的域名:"
    local san_info=$(openssl x509 -in "$cert_file" -noout -text 2>/dev/null | grep -A1 "Subject Alternative Name:" | tail -1)
    if [ -n "$san_info" ]; then
        echo "   $san_info" | tr ',' '\n' | sed 's/DNS://g' | sed 's/IP Address://g' | sed 's/^/     🌐 /'
    else
        # 如果没有SAN，则显示CN
        local cn=$(openssl x509 -in "$cert_file" -noout -subject 2>/dev/null | grep -o 'CN = [^,]*' | cut -d= -f2 | xargs)
        echo "     🌐 $cn"
    fi
    echo ""
    
    echo "🔐 密钥信息:"
    local key_type=$(openssl x509 -in "$cert_file" -noout -text 2>/dev/null | grep "Public Key Algorithm:" | cut -d: -f2 | xargs)
    local key_size=$(openssl x509 -in "$cert_file" -noout -text 2>/dev/null | grep "Public-Key:" | awk '{print $2}')
    
    echo "   🔸 算法: $key_type"
    echo "   🔸 密钥长度: ${key_size:-未知} 位"
    echo ""
    
    echo "🛡️ 证书扩展:"
    echo "   🔸 密钥用法:"
    openssl x509 -in "$cert_file" -noout -text 2>/dev/null | grep -A2 "X509v3 Key Usage" | tail -1 | sed 's/^/        📋 /'
    echo "   🔸 扩展密钥用法:"
    openssl x509 -in "$cert_file" -noout -text 2>/dev/null | grep -A2 "X509v3 Extended Key Usage" | tail -1 | sed 's/^/        📋 /'
    echo ""
    
    echo "🔢 证书序列号:"
    local serial=$(openssl x509 -in "$cert_file" -noout -serial 2>/dev/null | cut -d= -f2)
    echo "   🔢 $serial"
    echo ""
    
    echo "🔍 证书指纹:"
    echo "   🔸 MD5:"
    openssl x509 -in "$cert_file" -noout -fingerprint -md5 2>/dev/null | cut -d= -f2 | sed 's/^/        👆 /'
    echo "   🔸 SHA1:"
    openssl x509 -in "$cert_file" -noout -fingerprint -sha1 2>/dev/null | cut -d= -f2 | sed 's/^/        👆 /'
    echo "   🔸 SHA256:"
    openssl x509 -in "$cert_file" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2 | sed 's/^/        👆 /'
    echo ""
    
    # 有趣的证书评级
    echo "⭐ 证书评级:"
    local days_valid=$(( (expire_seconds - $(date -d "$not_before" +%s 2>/dev/null || date -j -f "%b %d %T %Y" "$not_before" +%s 2>/dev/null)) / 86400 ))
    
    if [ $days_valid -gt 365000 ]; then
        echo "   🌟🌟🌟🌟🌟 五星神级证书！"
        echo "   🎊 这证书能用到公元 $(date -d "$not_after" +%Y 2>/dev/null || date -j -f "%b %d %T %Y" "$not_after" +%Y 2>/dev/null) 年！"
        echo "   👑 您的后代都会感谢您的！"
    elif [ $days_valid -gt 36500 ]; then
        echo "   🌟🌟🌟🌟 四星优秀证书！"
        echo "   👍 能用 100 年以上，非常不错！"
    elif [ $days_valid -gt 3650 ]; then
        echo "   🌟🌟🌟 三星良好证书！"
        echo "   👌 10 年以上有效期，够用了！"
    elif [ $days_valid -gt 365 ]; then
        echo "   🌟🌟 二星标准证书！"
        echo "   📅 1 年以上有效期，符合标准！"
    else
        echo "   🌟 一星短期证书！"
        echo "   ⏰ 有效期较短，记得及时续期！"
    fi
    echo ""
    
    echo "💡 小贴士:"
    local current_year=$(date +%Y)
    local expire_year=$(date -d "$not_after" +%Y 2>/dev/null || date -j -f "%b %d %T %Y" "$not_after" +%Y 2>/dev/null)
    
    if [ -n "$expire_year" ] && [ "$expire_year" -gt 2100 ]; then
        echo "   🚀 这个证书能用到 $expire_year 年！"
        echo "   🌌 那时人类可能已经在火星定居了！"
    elif [ -n "$expire_year" ] && [ "$expire_year" -gt 2050 ]; then
        echo "   📡 这个证书能用到 $expire_year 年！"
        echo "   🤖 那时人工智能可能已经统治世界了！"
    elif [ -n "$expire_year" ] && [ "$expire_year" -gt 2030 ]; then
        echo "   📱 这个证书能用到 $expire_year 年！"
        echo "   🎮 那时可能已经有脑机接口游戏了！"
    else
        echo "   ⏳ 记得在过期前续期证书哦！"
    fi
    echo ""
    
    echo "🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉"
    echo "🎉        报告结束 - 希望您玩得开心！         🎉"
    echo "🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉"
    echo ""
}

# SSL证书验证函数
validate_ssl_certificate() {
    local cert_file="$1"
    local key_file="$2"
    
    echo "🔍 Validating SSL certificate and key..."
    
    # 1. 检查文件是否存在
    if [ ! -f "$cert_file" ]; then
        echo "❌ SSL certificate file not found: $cert_file"
        return 1
    fi
    
    if [ ! -f "$key_file" ]; then
        echo "❌ SSL key file not found: $key_file"
        return 1
    fi
    
    # 2. 检查证书格式是否有效
    echo "   Checking certificate format..."
    if ! openssl x509 -in "$cert_file" -noout 2>/dev/null; then
        echo "❌ Invalid certificate format: $cert_file"
        echo "   The certificate must be in PEM format"
        return 1
    fi
    
    # 3. 检查私钥格式是否有效
    echo "   Checking private key format..."
    local key_type="unknown"
    
    # 尝试确定密钥类型
    if openssl rsa -in "$key_file" -noout 2>/dev/null; then
        key_type="rsa"
        echo "   Key type: RSA"
    elif openssl ec -in "$key_file" -noout 2>/dev/null; then
        key_type="ec"
        echo "   Key type: EC"
    elif openssl pkey -in "$key_file" -noout 2>/dev/null; then
        key_type="pkey"
        echo "   Key type: Generic"
    else
        echo "❌ Invalid private key format: $key_file"
        echo "   The key must be in PEM format (RSA or EC)"
        return 1
    fi
    
    # 4. 检查证书和密钥是否匹配
    echo "   Checking certificate-key pair match..."
    
    if [ "$key_type" = "rsa" ]; then
        # RSA密钥验证
        cert_modulus=$(openssl x509 -in "$cert_file" -noout -modulus 2>/dev/null | openssl md5)
        key_modulus=$(openssl rsa -in "$key_file" -noout -modulus 2>/dev/null | openssl md5 2>/dev/null)
        
        if [ -z "$cert_modulus" ] || [ -z "$key_modulus" ]; then
            echo "❌ Failed to extract modulus from certificate or key"
            return 1
        fi
        
        if [ "$cert_modulus" = "$key_modulus" ]; then
            echo "✅ Certificate and RSA key match"
            return 0
        else
            echo "❌ Certificate and RSA key do not match!"
            echo "   Certificate modulus: $cert_modulus"
            echo "   Key modulus: $key_modulus"
            return 1
        fi
        
    elif [ "$key_type" = "ec" ] || [ "$key_type" = "pkey" ]; then
        # EC或通用密钥验证
        cert_pubkey=$(openssl x509 -in "$cert_file" -pubkey -noout 2>/dev/null)
        key_pubkey=$(openssl pkey -in "$key_file" -pubout 2>/dev/null)
        
        if [ -z "$cert_pubkey" ] || [ -z "$key_pubkey" ]; then
            echo "❌ Failed to extract public key from certificate or key"
            return 1
        fi
        
        # 清理公钥字符串以便比较
        cert_pubkey_clean=$(echo "$cert_pubkey" | sed '/^-----/d' | tr -d '\n')
        key_pubkey_clean=$(echo "$key_pubkey" | sed '/^-----/d' | tr -d '\n')
        
        if [ "$cert_pubkey_clean" = "$key_pubkey_clean" ]; then
            echo "✅ Certificate and $key_type key match"
            return 0
        else
            echo "❌ Certificate and $key_type key do not match!"
            return 1
        fi
    fi
    
    return 1
}

# 生成自签名SSL证书函数
generate_ssl_certificate() {
    local cert_file="$1"
    local key_file="$2"
    local domain="$3"
    local days="$4"
    
    echo "🔐 Generating self-signed SSL certificate for $domain..."
    echo "   Validity: $days days (~$(($days/365)) years)"
    
    # 创建证书目录
    mkdir -p "$(dirname "$cert_file")"
    
    # 生成RSA私钥（4096位）
    echo "   Generating RSA private key (4096 bits)..."
    openssl genrsa -out "$key_file" 4096 2>/dev/null
    
    if [ $? -ne 0 ] || [ ! -f "$key_file" ]; then
        echo "❌ Failed to generate private key"
        return 1
    fi
    
    # 创建OpenSSL配置文件，包含完整的扩展
    local openssl_config="/tmp/openssl.cnf"
    cat > "$openssl_config" <<EOF
[req]
default_bits = 4096
prompt = no
default_md = sha256
x509_extensions = v3_ca
distinguished_name = dn

[dn]
C = CN
ST = Beijing
L = Beijing
O = Project N.E.K.O
CN = $domain

[v3_ca]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, digitalSignature, keyEncipherment, keyCertSign
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = $domain
DNS.2 = *.$domain
DNS.3 = localhost
IP.1 = 127.0.0.1
EOF
    
    # 生成自签名证书
    echo "   Generating self-signed certificate..."
    openssl req -new -x509 \
        -key "$key_file" \
        -out "$cert_file" \
        -days "$days" \
        -sha256 \
        -config "$openssl_config" \
        2>/dev/null
    
    if [ $? -ne 0 ] || [ ! -f "$cert_file" ]; then
        echo "❌ Failed to generate certificate"
        rm -f "$openssl_config"
        return 1
    fi
    
    # 清理临时文件
    rm -f "$openssl_config"
    
    # 验证生成的证书
    echo "   Verifying generated certificate..."
    if ! openssl x509 -in "$cert_file" -noout -text 2>/dev/null | grep -q "CN=$domain"; then
        echo "❌ Generated certificate does not contain expected domain"
        return 1
    fi
    
    # 检查证书扩展
    echo "   Checking certificate extensions..."
    if ! openssl x509 -in "$cert_file" -noout -text 2>/dev/null | grep -q "X509v3 Key Usage"; then
        echo "❌ Generated certificate missing Key Usage extension"
        return 1
    fi
    
    if ! openssl x509 -in "$cert_file" -noout -text 2>/dev/null | grep -q "X509v3 Extended Key Usage"; then
        echo "❌ Generated certificate missing Extended Key Usage extension"
        return 1
    fi
    
    # 设置正确的文件权限
    chmod 600 "$key_file"
    chmod 644 "$cert_file"
    
    echo "✅ SSL certificate generated successfully"
    echo "   Domain: $domain"
    echo "   Certificate: $cert_file"
    echo "   Private Key: $key_file"
    
    return 0
}

# setup_nginx_proxy sets up and writes the Nginx main and site configuration for the container, creating proxy rules (including WebSocket support), static file serving, a health endpoint, removes the client request body size limit, and validates the resulting configuration.
setup_nginx_proxy() {
    echo "🌐 Setting up Nginx reverse proxy..."
    
    # 创建必要的日志目录
    mkdir -p /var/log/nginx
    
    # 生成SSL证书和密钥（如果不存在）
    echo "🔐 Setting up SSL certificates..."
    mkdir -p /home/neko/ssl
    
    local cert_file="/home/neko/ssl/N.E.K.O.crt"
    local key_file="/home/neko/ssl/N.E.K.O.key"
    
    # 如果证书或密钥不存在，直接生成新的
    if [ ! -f "$cert_file" ] || [ ! -f "$key_file" ]; then
        echo "🔐 SSL certificate or key not found. Generating new certificate..."
        
        # 如果存在不完整文件，先删除
        rm -f "$cert_file" "$key_file"
        
        # 生成自签名SSL证书
        if ! generate_ssl_certificate "$cert_file" "$key_file" "$SSL_DOMAIN" "$SSL_DAYS"; then
            echo "❌ Failed to generate SSL certificate"
            if [ "${DISABLE_SSL:-0}" != "1" ]; then
                exit 1
            fi
        fi
        
    else
        echo "🔐 Using existing SSL certificate and key"
    fi
    
    # 验证SSL证书和密钥
    if [ -f "$cert_file" ] && [ -f "$key_file" ]; then
        if ! validate_ssl_certificate "$cert_file" "$key_file"; then
            echo "❌ SSL certificate validation failed."
            
            # 如果验证失败，询问是否重新生成证书
            if [ "${AUTO_REGENERATE_CERT:-1}" = "1" ]; then
                echo "🔄 Auto-regenerating SSL certificate..."
                rm -f "$cert_file" "$key_file"
                
                if generate_ssl_certificate "$cert_file" "$key_file" "$SSL_DOMAIN" "$SSL_DAYS"; then
                    echo "✅ Successfully regenerated SSL certificate"
                else
                    echo "❌ Failed to regenerate valid SSL certificate"
                    if [ "${DISABLE_SSL:-0}" != "1" ]; then
                        exit 1
                    fi
                fi
            else
                echo "❌ SSL certificate validation failed. Please check your certificate and key files."
                echo "   Certificate: $cert_file"
                echo "   Key: $key_file"
                echo "   You can either:"
                echo "   1. Fix the certificate/key files"
                echo "   2. Remove them to let the script generate new ones"
                echo "   3. Set environment variable DISABLE_SSL=1 to skip SSL"
                if [ "${DISABLE_SSL:-0}" != "1" ]; then
                    exit 1
                else
                    echo "⚠️ SSL disabled, continuing without HTTPS..."
                fi
            fi
        fi
    else
        echo "❌ SSL certificate or key file missing"
        if [ "${DISABLE_SSL:-0}" != "1" ]; then
            exit 1
        fi
    fi
    
    # 输出详细的证书信息（就是为了图一乐！）
    if [ "${DISABLE_SSL:-0}" != "1" ] && [ -f "$cert_file" ]; then
        print_certificate_fun_info "$cert_file"
    fi
    
    # 生成主要的Nginx配置文件
    cat > /etc/nginx/nginx.conf <<EOF
worker_processes auto;
error_log /var/log/nginx/error.log notice;
pid /var/run/nginx.pid;

events {
    worker_connections 1024;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    
    log_format main '\$remote_addr - \$remote_user [\$time_local] "\$request" '
                    '\$status \$body_bytes_sent "\$http_referer" '
                    '"\$http_user_agent" "\$http_x_forwarded_for"';
    
    access_log /var/log/nginx/access.log main;
    
    sendfile on;
    tcp_nopush on;
    keepalive_timeout 65;
    types_hash_max_size 2048;
    
    # 包含我们的代理配置
    include /etc/nginx/conf.d/*.conf;
}
EOF
    
    # 根据是否启用SSL生成不同的配置
    if [ "${DISABLE_SSL:-0}" = "1" ]; then
        echo "🌐 Generating HTTP-only configuration (SSL disabled)..."
        cat > /etc/nginx/conf.d/neko-proxy.conf <<EOF
server {
    listen ${NGINX_PORT};
    server_name _;
    
    # 禁用默认的Nginx版本显示
    server_tokens off;
    
    # 取消客户端请求体大小限制
    client_max_body_size 0;

    # 代理到用户插件服务 (Plugin Server, 内嵌于 agent_server 进程)
    location ~ ^/(api/model-config(?:/|$)|ui|plugins?|plugin/|available|server/|logs/|metrics|runs|packages|plugin-cli/|market/|health|market-bridge/) {
        proxy_pass http://127.0.0.1:48916;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 86400;
    }

    # 插件 WebSocket 路径 → 插件服务器 (48916)
    location ~ ^/ws/(run|admin|logs)(/|$) {
        proxy_pass http://127.0.0.1:48916;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 86400;
    }
    
    # 代理到N.E.K.O主服务
    location / {
        proxy_pass http://127.0.0.1:${NEKO_MAIN_SERVER_PORT};
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        
        # WebSocket支持
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # 超时设置
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 86400;  # 长超时用于WebSocket
    }
    
    # 代理到记忆服务
    location /memory/ {
        proxy_pass http://127.0.0.1:48912;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    
    # 代理到Agent服务
    location /agent/ {
        proxy_pass http://127.0.0.1:48915;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    
    # 静态文件服务
    location /static/ {
        alias /app/static/;
        expires 30d;
        add_header Cache-Control "public, immutable";
        try_files \$uri \$uri/ =404;
    }
    
    # 健康检查端点
    location /health {
        access_log off;
        return 200 "healthy\n";
        add_header Content-Type text/plain;
    }
    
    # 阻止访问隐藏文件
    location ~ /\. {
        deny all;
        access_log off;
        log_not_found off;
    }
}
EOF
    else
        echo "🌐 Generating HTTP+HTTPS configuration..."
        cat > /etc/nginx/conf.d/neko-proxy.conf <<EOF
server {
    listen ${NGINX_PORT};
    listen ${NGINX_SSL_PORT} ssl http2;
    
    # SSL证书配置（仅对443端口生效）
    ssl_certificate $cert_file;
    ssl_certificate_key $key_file;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;
    ssl_ciphers ECDHE-RSA-AES256-GCM-SHA512:DHE-RSA-AES256-GCM-SHA512:ECDHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES256-GCM-SHA384;
    
    # 设置HSTS头（增强安全性）
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    
    server_name _;
    
    # 禁用默认的Nginx版本显示
    server_tokens off;
    
    # 取消客户端请求体大小限制
    client_max_body_size 0;

    # 代理到用户插件服务 (Plugin Server, 内嵌于 agent_server 进程)
    location ~ ^/(api/model-config(?:/|$)|ui|plugins?|plugin/|available|server/|logs/|metrics|runs|packages|plugin-cli/|market/|health|market-bridge/) {
        proxy_pass http://127.0.0.1:48916;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 86400;
    }

    # 插件 WebSocket 路径 → 插件服务器 (48916)
    location ~ ^/ws/(run|admin|logs)(/|$) {
        proxy_pass http://127.0.0.1:48916;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 86400;
    }
    
    # 代理到N.E.K.O主服务
    location / {
        proxy_pass http://127.0.0.1:${NEKO_MAIN_SERVER_PORT};
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        
        # WebSocket支持
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        
        # 超时设置
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 86400;  # 长超时用于WebSocket
    }
    
    # 代理到记忆服务
    location /memory/ {
        proxy_pass http://127.0.0.1:48912;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    
    # 代理到Agent服务
    location /agent/ {
        proxy_pass http://127.0.0.1:48915;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
    
    # 静态文件服务
    location /static/ {
        alias /app/static/;
        expires 30d;
        add_header Cache-Control "public, immutable";
        try_files \$uri \$uri/ =404;
    }
    
    # 健康检查端点
    location /health {
        access_log off;
        return 200 "healthy\n";
        add_header Content-Type text/plain;
    }
    
    # 阻止访问隐藏文件
    location ~ /\. {
        deny all;
        access_log off;
        log_not_found off;
    }
}
EOF
    fi
    
    # 测试Nginx配置
    echo "🔧 Testing Nginx configuration..."
    if nginx -t; then
        echo "✅ Nginx configuration is valid"
    else
        echo "❌ Nginx configuration test failed"
        # 显示详细的错误信息
        nginx -t 2>&1
        exit 1
    fi
}

# 4. Nginx热重载器函数
start_nginx_reloader() {
    if [ "${NGINX_AUTO_RELOAD}" != "1" ]; then
        echo "⚠️ Nginx auto-reloader disabled"
        return 0
    fi
    
    echo "🔄 Starting Nginx configuration auto-reloader (every 5 minutes)..."
    
    # 记录初始配置文件的修改时间
    local nginx_conf="/etc/nginx/nginx.conf"
    local site_conf="/etc/nginx/conf.d/neko-proxy.conf"
    local ssl_cert="/home/neko/ssl/N.E.K.O.crt"
    local ssl_key="/home/neko/ssl/N.E.K.O.key"
    
    local last_conf_time=$(stat -c %Y "$nginx_conf" "$site_conf" 2>/dev/null)
    local last_ssl_time=""
    
    if [ -f "$ssl_cert" ] && [ -f "$ssl_key" ]; then
        last_ssl_time=$(stat -c %Y "$ssl_cert" "$ssl_key" 2>/dev/null)
    fi
    
    # 热重载循环
    while true; do
        sleep 300  # 每5分钟检查一次
        
        echo "🔄 Checking for configuration changes..."
        
        local changed=0
        
        # 检查Nginx配置文件是否变化
        local current_conf_time=$(stat -c %Y "$nginx_conf" "$site_conf" 2>/dev/null)
        if [ "$current_conf_time" != "$last_conf_time" ]; then
            echo "📄 Nginx configuration files changed"
            changed=1
            last_conf_time="$current_conf_time"
        fi
        
        # 检查SSL证书文件是否变化（如果SSL启用）
        if [ "${DISABLE_SSL:-0}" != "1" ] && [ -f "$ssl_cert" ] && [ -f "$ssl_key" ]; then
            local current_ssl_time=$(stat -c %Y "$ssl_cert" "$ssl_key" 2>/dev/null)
            if [ "$current_ssl_time" != "$last_ssl_time" ]; then
                echo "🔐 SSL certificate files changed"
                changed=1
                last_ssl_time="$current_ssl_time"
                
                # 重新验证SSL证书
                if validate_ssl_certificate "$ssl_cert" "$ssl_key"; then
                    echo "✅ SSL certificate validation passed"
                else
                    echo "❌ SSL certificate validation failed"
                    echo "   Skipping reload until certificate issues are fixed"
                    continue
                fi
            fi
        fi
        
        # 如果有变化，执行热重载
        if [ "$changed" -eq 1 ]; then
            echo "🔄 Configuration changed, reloading Nginx..."
            
            # 测试Nginx配置
            if nginx -t; then
                echo "✅ Nginx configuration test passed"
                
                # 执行热重载
                if nginx -s reload; then
                    echo "✅ Nginx successfully reloaded"
                else
                    echo "❌ Nginx reload failed, trying restart..."
                    # 如果热重载失败，尝试重启
                    nginx -s quit 2>/dev/null || true
                    sleep 2
                    nginx -g "daemon off;" &
                    local nginx_pid=$!
                    # 更新Nginx PID
                    for i in "${!PIDS[@]}"; do
                        if [[ "${PIDS[$i]}" =~ ^[0-9]+$ ]] && ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
                            PIDS[$i]=$nginx_pid
                            echo "✅ Nginx restarted with PID: $nginx_pid"
                            break
                        fi
                    done
                fi
            else
                echo "❌ Nginx configuration test failed, skipping reload"
                echo "   Check configuration with: nginx -t"
            fi
        else
            echo "✅ No configuration changes detected"
        fi
    done
}

# 5. 配置管理优化
migrate_legacy_bootstrap_config() {
    # Older images generated this file below /app, which is normally a
    # container-writable layer rather than a persistent mount.  When it is
    # still available (for example an operator retains an old /app/config
    # mount), seed the new runtime root before setup_configuration decides
    # whether a bootstrap file exists.  An explicit force update intentionally
    # wins over this compatibility path.
    local LEGACY_CONFIG_FILE="/app/config/core_config.json"
    local CONFIG_ROOT="${NEKO_STORAGE_SELECTED_ROOT:-${XDG_DATA_HOME:-/home/neko/.local/share}/N.E.K.O}"
    local RUNTIME_CONFIG_FILE="$CONFIG_ROOT/config/core_config.json"
    local RUNTIME_CONFIG_DIR="$(dirname "$RUNTIME_CONFIG_FILE")"
    local TEMP_CONFIG_FILE=""

    if [ -n "${NEKO_FORCE_ENV_UPDATE:-}" ] || [ -f "$RUNTIME_CONFIG_FILE" ] || [ ! -f "$LEGACY_CONFIG_FILE" ]; then
        return 0
    fi

    mkdir -p "$RUNTIME_CONFIG_DIR"
    if ! TEMP_CONFIG_FILE=$(mktemp "$RUNTIME_CONFIG_DIR/.core_config.json.migrate.XXXXXX"); then
        echo "❌ Failed to create a temporary file for legacy configuration migration"
        return 1
    fi
    if ! cp "$LEGACY_CONFIG_FILE" "$TEMP_CONFIG_FILE"; then
        rm -f "$TEMP_CONFIG_FILE"
        echo "❌ Failed to copy legacy configuration into the persistent runtime root"
        return 1
    fi
    if ! mv -f "$TEMP_CONFIG_FILE" "$RUNTIME_CONFIG_FILE"; then
        rm -f "$TEMP_CONFIG_FILE"
        echo "❌ Failed to finalize legacy configuration migration"
        return 1
    fi
    echo "📦 Migrated legacy /app/config/core_config.json into the persistent runtime root"
}

setup_configuration() {
    echo "📝 Setting up configuration..."
    # Keep the bootstrap file in the same runtime root selected by
    # ConfigManager.  /app/config belongs to the image layer and would be lost
    # whenever the container is recreated.
    local CONFIG_ROOT="${NEKO_STORAGE_SELECTED_ROOT:-${XDG_DATA_HOME:-/home/neko/.local/share}/N.E.K.O}"
    local CONFIG_DIR="$CONFIG_ROOT/config"
    local CORE_CONFIG_FILE="$CONFIG_DIR/core_config.json"
    
    mkdir -p "$CONFIG_DIR"
    
    # 只有在配置文件不存在或强制更新时才生成
    if [ ! -f "$CORE_CONFIG_FILE" ] || [ -n "${NEKO_FORCE_ENV_UPDATE}" ]; then
        cat > "$CORE_CONFIG_FILE" <<EOF
{
  "coreApiKey": "${NEKO_CORE_API_KEY:-}",
  "coreApi": "${NEKO_CORE_API:-qwen}",
  "assistApi": "${NEKO_ASSIST_API:-qwen}",
  "assistApiKeyQwen": "${NEKO_ASSIST_API_KEY_QWEN:-}",
  "assistApiKeyOpenai": "${NEKO_ASSIST_API_KEY_OPENAI:-}",
  "assistApiKeyGlm": "${NEKO_ASSIST_API_KEY_GLM:-}",
  "assistApiKeyStep": "${NEKO_ASSIST_API_KEY_STEP:-}",
  "assistApiKeySilicon": "${NEKO_ASSIST_API_KEY_SILICON:-}",
  "assistApiKeyGrok": "${NEKO_ASSIST_API_KEY_GROK:-}",
  "assistApiKeyDoubao": "${NEKO_ASSIST_API_KEY_DOUBAO:-}",
  "mcpToken": "${NEKO_MCP_TOKEN:-}"
}
EOF
        echo "✅ Configuration file created/updated"
    else
        echo "📄 Using existing configuration"
    fi
    
    # 安全显示配置（隐藏敏感信息）
    echo "🔧 Runtime Configuration:"
    echo "   Core API: ${NEKO_CORE_API:-qwen}"
    echo "   Assist API: ${NEKO_ASSIST_API:-qwen}"
    echo "   Main Server Port: ${NEKO_MAIN_SERVER_PORT:-48911}"
    echo "   Nginx HTTP Port: ${NGINX_PORT}"
    echo "   Nginx HTTPS Port: ${NGINX_SSL_PORT}"
    echo "   SSL Domain: ${SSL_DOMAIN}"
    echo "   SSL Validity: ${SSL_DAYS} days (~$(($SSL_DAYS/365)) years)"
    echo "   SSL Enabled: $([ "${DISABLE_SSL:-0}" = "1" ] && echo "No" || echo "Yes")"
    echo "   Auto-regenerate cert: $([ "${AUTO_REGENERATE_CERT:-1}" = "1" ] && echo "Yes" || echo "No")"
    echo "   Nginx auto-reload: $([ "${NGINX_AUTO_RELOAD:-1}" = "1" ] && echo "Enabled (every 5 min)" || echo "Disabled")"
}

# 6. 依赖管理优化
setup_dependencies() {
    echo "📦 Setting up dependencies..."
    cd /app
    
    # 激活虚拟环境（如果存在）
    if [ -f ".venv/bin/activate" ]; then
        source .venv/bin/activate
    fi
    
    # 使用uv sync安装依赖
    echo "   Installing Python dependencies using uv..."
    
    # 检查是否存在uv.lock
    if [ -f "uv.lock" ]; then
        uv sync
    else
        # 如果没有锁定文件，尝试初始化
        if [ -f "pyproject.toml" ]; then
            uv sync
        else
            echo "⚠️ No pyproject.toml found. Initializing project..."
            uv init --non-interactive
            uv sync
        fi
    fi
    
    # uv sync 以 root 运行，生成的文件归 root；服务以 neko 用户运行，须修正权限
    echo "🔧 Fixing .venv permissions for neko user..."
    chown -R neko:neko /app/.venv 2>/dev/null || echo "⚠️ Could not chown .venv (non-fatal)"
    # 同时确保文档和模板目录可被 neko 读取（用于 OpenClaw 指南等静态内容）
    chown -R neko:neko /app/docs /app/templates /app/static 2>/dev/null || echo "⚠️ Could not chown doc/template/static dirs (non-fatal)"
    
    echo "✅ Dependencies installed successfully"
    
    # 安装 Playwright 浏览器（用于 browser_use 自动化）
    # 如果浏览器已预装（如 full 镜像），则跳过安装
    echo "🎭 Checking Playwright Chromium browser..."
    mkdir -p "${PLAYWRIGHT_BROWSERS_PATH:-/opt/ms-playwright}"
    
    # 检查是否已有 Chromium 浏览器安装
    if ls "${PLAYWRIGHT_BROWSERS_PATH:-/opt/ms-playwright}"/chromium-* 1>/dev/null 2>&1; then
        echo "✅ Playwright Chromium already installed, skipping installation"
    else
        echo "📦 Playwright Chromium not found, installing..."
        # 尝试安装 Chromium，失败时不中断启动
        if uv run --no-sync python -m playwright install chromium --with-deps 2>&1; then
            echo "✅ Playwright Chromium installed successfully"
            echo "🔧 Fixing Playwright browser permissions for neko user..."
            chown -R neko:neko "${PLAYWRIGHT_BROWSERS_PATH:-/opt/ms-playwright}" 2>/dev/null || echo "⚠️ Could not chown Playwright browsers (non-fatal)"
        else
            echo "⚠️ Playwright Chromium installation failed (will retry on next start)"
            echo "   Browser automation features may not work until Playwright is installed"
        fi
    fi
}

# 7. 服务启动优化
start_services() {
    echo "🚀 Starting N.E.K.O. services..."
    cd /app
    
    # Use the virtual environment's Python interpreter
    local VENV_PYTHON="/app/.venv/bin/python"
    
    # Verify venv exists
    if [ ! -f "$VENV_PYTHON" ]; then
        echo "❌ Virtual environment not found at /app/.venv!"
        echo "   Attempting to use system python3 as fallback..."
        VENV_PYTHON="python3"
    fi
    
    # PR #1265: 4 个 server 入口搬进 app/ 子包；这里跟着改成 app/<name>.py
    # PR #2265: agent_server 拆成包（app/agent_server/），存在性检查落在包的
    # __main__.py 上；启动命令对包用 python -m（直跑 __main__.py 会丢 repo
    # 根的 sys.path，config 等顶层包会 import 不到）。
    # PR #2264: memory_server 同样包化（app/memory_server/），走同一机制；
    # 其 __main__.py 顶部另带 sys.path bootstrap，文件直跑亦等价，但统一用 -m。
    local services=("app/memory_server/__main__.py" "app/main_server/__main__.py" "app/agent_server/__main__.py")

    for service in "${services[@]}"; do
        if [ ! -f "$service" ]; then
            echo "❌ Service file $service not found!"
            # 对关键服务直接失败
            if [[ "$service" == "app/main_server/__main__.py" ]] || [[ "$service" == "app/memory_server/__main__.py" ]]; then
                return 1
            fi
            continue
        fi

        echo "   Starting $service as neko user..."
        # 启动服务并记录PID（以 neko 用户运行，使用 venv 的 Python）
        if [[ "$service" == "app/agent_server/__main__.py" ]]; then
            runuser -u neko -- "$VENV_PYTHON" -m app.agent_server &
        elif [[ "$service" == "app/memory_server/__main__.py" ]]; then
            runuser -u neko -- "$VENV_PYTHON" -m app.memory_server &
        elif [[ "$service" == "app/main_server/__main__.py" ]]; then
            runuser -u neko -- "$VENV_PYTHON" -m app.main_server &
        else
            runuser -u neko -- "$VENV_PYTHON" "$service" &
        fi
        local pid=$!
        PIDS+=("$pid")
        echo "     Started $service with PID: $pid (running as neko)"
        sleep 5  # 给服务启动留出更多时间
    done
    
    # 健康检查
    echo "🔍 Performing health checks..."
    sleep 15
    
    # 检查进程是否运行
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "✅ Process $pid is running"
        else
            echo "❌ Process $pid failed to start"
            return 1
        fi
    done
    
    # 检查主服务端口（内部检查）
    if command -v ss &> /dev/null; then
        if ss -tuln | grep -q ":${NEKO_MAIN_SERVER_PORT} "; then
            echo "✅ Main server is listening on port ${NEKO_MAIN_SERVER_PORT}"
        else
            echo "❌ Main server failed to bind to port"
            return 1
        fi
    else
        echo "⚠️ Port check skipped (ss command not available)"
    fi
    
    echo "🎉 All N.E.K.O services started successfully!"
}

# 8. 启动Nginx代理
start_nginx_proxy() {
    echo "🌐 Starting Nginx reverse proxy..."
    
    # 启动Nginx
    nginx -g "daemon off;" &
    local nginx_pid=$!
    PIDS+=("$nginx_pid")
    
    sleep 3
    
    # 检查Nginx是否运行
    if kill -0 "$nginx_pid" 2>/dev/null; then
        echo "✅ Nginx is running with PID: $nginx_pid"
    else
        echo "❌ Nginx failed to start"
        return 1
    fi
    
    # 检查Nginx端口
    if command -v ss &> /dev/null; then
        echo "🔌 Checking HTTP port (${NGINX_PORT})..."
        if ss -tuln | grep -q ":${NGINX_PORT} "; then
            echo "✅ Nginx is listening on HTTP port ${NGINX_PORT}"
        else
            echo "❌ Nginx failed to bind to HTTP port ${NGINX_PORT}"
            return 1
        fi
        
        if [ "${DISABLE_SSL:-0}" != "1" ]; then
            echo "🔌 Checking HTTPS port (${NGINX_SSL_PORT})..."
            if ss -tuln | grep -q ":${NGINX_SSL_PORT} "; then
                echo "✅ Nginx is listening on HTTPS port ${NGINX_SSL_PORT}"
            else
                echo "❌ Nginx failed to bind to HTTPS port ${NGINX_SSL_PORT}"
                return 1
            fi
        fi
    fi
    
    echo "🌐 Nginx proxy accessible at:"
    echo "   HTTP: http://localhost:${NGINX_PORT}"
    if [ "${DISABLE_SSL:-0}" != "1" ]; then
        echo "   HTTPS: https://localhost:${NGINX_SSL_PORT}"
    else
        echo "   HTTPS: Disabled"
    fi
    echo "📊 Original service at: http://127.0.0.1:${NEKO_MAIN_SERVER_PORT}"
    
    # 启动Nginx热重载器
    if [ "${NGINX_AUTO_RELOAD}" = "1" ]; then
        start_nginx_reloader &
        RELOADER_PID=$!
        echo "🔄 Nginx auto-reloader started with PID: $RELOADER_PID"
    fi
    
    return 0
}

# ── 旧版挂载布局检测 ──────────────────────────────────────────
# 数据挂载从 ./N.E.K.O + ./ssl 两条合并成了 ./neko-home 一条。被落下的旧目录在
# 宿主机上，根本没挂进容器，所以这里无法自动迁移；能做的只是在认出「这是一次升级」
# 时把话说清楚，免得用户把一个空数据目录当成正常的全新安装。仍可访问的旧
# /app/config/core_config.json 会被迁移；已经随旧容器删除的那份则无法自动恢复。
# 快照必须在本脚本自己往 /home/neko 和 /app/logs 写东西之前取。
LEGACY_LAYOUT_HINT=""

detect_legacy_layout() {
    local data_root="/home/neko/.local/share/N.E.K.O"

    # 放错位置的旧目录先查，且不拿「数据根是否为空」当前提。用户很可能是先用新版
    # 起了一次（应用于是往数据根写了一份默认配置），发现东西不见了才去搬旧目录，
    # 而后搬错层 —— 那正是最该提醒的时刻，此时数据根恰恰是非空的。这两个目录名
    # 应用自己永远不会在家目录根下创建，出现即是人为放置。
    #
    # 痕迹一：旧的 N.E.K.O 目录被整个当成了 neko-home，数据根的子目录直接躺在家目录里
    if [ -d /home/neko/config ] || [ -d /home/neko/memory ]; then
        LEGACY_LAYOUT_HINT="wrong-level"
        return 0
    fi

    # 痕迹二：旧目录搬进 neko-home 了，但没落到 XDG 路径下
    if [ -d /home/neko/N.E.K.O ]; then
        LEGACY_LAYOUT_HINT="wrong-depth"
        return 0
    fi

    # 剩下这条必须以数据根为空为前提：日志有历史 + 数据根有内容 = 正常运行中
    if [ -n "$(ls -A "$data_root" 2>/dev/null)" ]; then
        return 0
    fi

    # 痕迹三：./logs 里有历史日志 → 这个部署以前跑起来过，而数据目录却是空的。
    # 典型的「直接 pull 新镜像重启」，旧数据还留在宿主机的 ./N.E.K.O 里。
    if [ -n "$(find /app/logs -type f -name '*.log' 2>/dev/null | head -n 1)" ]; then
        LEGACY_LAYOUT_HINT="stale-logs"
    fi

    return 0
}

warn_legacy_layout() {
    if [ -z "$LEGACY_LAYOUT_HINT" ]; then
        return 0
    fi

    echo ""
    echo "=================================================="
    echo "⚠️  检测到旧版部署的痕迹"
    case "$LEGACY_LAYOUT_HINT" in
        wrong-level)
            echo "   /home/neko 下直接出现了 config/ 或 memory/ —— 旧的 N.E.K.O 目录"
            echo "   可能被整个当成了 neko-home。它应该放在下面这一层："
            echo "       neko-home/.local/share/N.E.K.O/"
            echo "   若那个位置已经有本次启动生成的默认配置，别直接覆盖，按内容合并："
            echo "       cp -a neko-home/config neko-home/memory ... neko-home/.local/share/N.E.K.O/"
            ;;
        wrong-depth)
            echo "   /home/neko/N.E.K.O 存在，但服务读的是 .local/share 下的路径。"
            echo "   请把它的内容并到：neko-home/.local/share/N.E.K.O/"
            echo "       cp -a neko-home/N.E.K.O/. neko-home/.local/share/N.E.K.O/ \\"
            echo "         && rm -rf neko-home/N.E.K.O"
            ;;
        stale-logs)
            echo "   日志目录里有历史日志，说明这个部署以前跑过，而数据目录是空的。"
            echo "   本版本把 ./N.E.K.O + ./ssl 两个挂载合并成了 ./neko-home 一个。"
            echo "   先看宿主机上的 N.E.K.O/ 是不是空的："
            echo "     · 非空 —— 数据还在，停容器后执行（直接 mv 会多套一层，本次"
            echo "       启动已经在 neko-home 下建好了同名目录）："
            echo "         mkdir -p neko-home/.local/share/N.E.K.O neko-home/ssl"
            echo "         cp -a N.E.K.O/. neko-home/.local/share/N.E.K.O/ && rm -rf N.E.K.O"
            echo "         cp -a ssl/.     neko-home/ssl/                 && rm -rf ssl"
            echo "     · 空 —— 此前跟的是旧版 README，其挂载目标从来对不上服务的实际"
            echo "       写入位置，数据只存在于旧容器里。若那个容器已被重建或删除，"
            echo "       这部分数据无法找回。"
            ;;
    esac
    echo "   详见 README「从旧版本升级」一节。全新安装可忽略本提示。"
    echo "=================================================="
    echo ""
}

# 9. 主执行流程
main() {
    echo "=================================================="
    echo "   N.E.K.O. Container with Nginx Proxy - Startup"
    echo "=================================================="

    # 先取快照：后面的 setup_* 会往 /home/neko 和 /app/logs 里写东西
    detect_legacy_layout

    setup_signal_handlers
    check_dependencies
    configure_timezone
    migrate_legacy_bootstrap_config
    setup_configuration
    setup_dependencies
    setup_nginx_proxy
    # 确保数据目录对 neko 用户可写（Docker volume 可能以 root 创建）。
    #
    # 只认应用真正要写的两棵子树，不扫整个 home。合并挂载之后 /home/neko 是宿主
    # 目录，用户完全可以往它下面再挂别的东西；递归整棵树意味着那些嵌套挂载的宿主
    # 侧对象会被改成 neko —— 挂进来的要是个 docker socket，等于把 docker 控制权
    # 交给以 neko 运行的业务进程和插件代码。（-xdev 挡不住：同一宿主文件系统的
    # bind mount 设备号相同。）
    #
    # /home/neko 本身只非递归地给一次：neko 要能在自己家里创建 .cache 之类的
    # 目录，否则运行期任何往 $HOME 写东西的库都会踩坑；但它下面挂了什么不归我们管。
    #
    # ssl/ 因此天然不在范围内。私钥由 root 生成并 chmod 600，nginx 主进程也以
    # root 读取，业务进程没有任何理由读得到它。
    #
    # 只改属主不对的条目：稳态下这一遍是纯遍历、不产生写入。chown 必须带 -h，
    # 否则它会解引用符号链接去改**目标**（`chown -R` 默认 -P 有这层保护，换成
    # find 驱动就没有了）。
    #
    # 失败必须停：三个业务进程都以 neko 跑，数据目录不可写的话它们会在运行期
    # 各处零散报错，比在这里干脆退出难诊断得多。
    #
    # 属主写字面量 1000:1000 而不是 neko:neko —— 两者在本镜像里是同一个（Dockerfile
    # 用 `useradd -u 1000 -g 1000` 固定住了），但这些路径是 bind mount 出去的，落到
    # 宿主机上只有数字有意义：宿主那边没有叫 neko 的账户，它看到的就是 1000，而 1000
    # 恰是绝大多数发行版第一个普通用户的号，对上之后用户不必 sudo 就能管自己的备份。
    # 写死数字也让这层契约在文件里是可见的：改 Dockerfile 里的号就必须同步改这里。
    # 状态目录不接受软链。两条 chown 都带了 -h（不带会解引用命令行参数去改目标），
    # 但那挡不住 find：路径中间那几段的符号链接是内核在解析路径时就跟随掉的，
    # find 根本无从拒绝，于是它会走到 /home/neko 之外的树上，把那里每个不属于
    # 1000 的条目都改掉。与其想办法处理，不如直接不收 —— 想把数据放到别的磁盘，
    # 该在 compose 里把 neko-home 挂到那个位置，而不是在里面做软链。
    for _state_dir in /home/neko/.local /home/neko/.local/share \
                      /home/neko/.local/share/N.E.K.O; do
        if [ -L "$_state_dir" ]; then
            echo "❌ $_state_dir 是符号链接，数据目录不支持这样放"
            echo "   启动时的属主修复会顺着它改到 /home/neko 之外的宿主路径上。"
            echo "   请把它换成真实目录；想让数据落在别的位置，就在 compose 里把"
            echo "   neko-home 直接挂到那个位置。"
            exit 1
        fi
    done
    unset _state_dir

    mkdir -p /home/neko/.local/share/N.E.K.O
    if ! chown -h 1000:1000 /home/neko /home/neko/.local /home/neko/.local/share \
        || ! find /home/neko/.local/share/N.E.K.O \
               \( ! -uid 1000 -o ! -gid 1000 \) -exec chown -h 1000:1000 {} + ; then
        echo "❌ 无法把数据目录的属主改为 1000:1000（容器内的 neko）"
        echo "   宿主机的挂载可能不允许改属主 —— NFS 带 root_squash、CIFS 没带"
        echo "   cifsacl、或只读挂载都会这样。容器内的服务以 neko 身份运行，"
        echo "   数据目录写不进去会在启动之后才零散暴露，所以这里直接停。"
        exit 1
    fi

    # 放在服务启动前打印：此时前面的初始化日志已经刷完，这条不会被淹掉
    warn_legacy_layout

    # 启动N.E.K.O服务
    if ! start_services; then
        echo "❌ Failed to start N.E.K.O services"
        exit 1
    fi
    
    # 启动Nginx代理
    if ! start_nginx_proxy; then
        echo "❌ Failed to start Nginx proxy"
        exit 1
    fi
    
    echo "🎉🎉 All systems operational!"
    echo " Project Address: https://github.com/Project-N-E-K-O/N.E.K.O"
    echo "🌐 Web UI accessible via:"
    echo "   HTTP: http://localhost:${NGINX_PORT}"
    if [ "${DISABLE_SSL:-0}" != "1" ]; then
        echo "   HTTPS: https://localhost:${NGINX_SSL_PORT}"
    fi
    echo "🔐 SSL Certificate:"
    echo "   Domain: ${SSL_DOMAIN}"
    echo "   Validity: ${SSL_DAYS} days (~$(($SSL_DAYS/365)) years)"
    echo "🔄 Nginx auto-reload: $([ "${NGINX_AUTO_RELOAD:-1}" = "1" ] && echo "Enabled (every 5 minutes)" || echo "Disabled")"
    echo "Use CTRL+C to stop all services"
    
    # 等待所有进程
    wait
}

# 执行主函数
main "$@"
