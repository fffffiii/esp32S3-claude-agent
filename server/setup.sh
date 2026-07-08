#!/bin/bash
# 一键安装 Mosquitto（支持 Alibaba Cloud Linux / CentOS / RHEL / Ubuntu）
set -euo pipefail

MQTT_USER="${MQTT_USER:-whitebox}"
MQTT_PASS="${MQTT_PASS:-change_me}"
CONF_DIR="/etc/mosquitto/conf.d"
CONF_FILE="${CONF_DIR}/whitebox.conf"
PASSWD_FILE="/etc/mosquitto/passwd"

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "请使用 root 用户执行：sudo bash setup.sh"
        exit 1
    fi
}

detect_system() {
    echo "=== 检测系统 ==="
    if [ -r /etc/os-release ]; then
        cat /etc/os-release
        # shellcheck disable=SC1091
        . /etc/os-release
    else
        echo "未找到 /etc/os-release，将按当前包管理器继续安装。"
        ID=""
        VERSION_ID=""
    fi
}

install_with_apt() {
    echo "=== 安装 Mosquitto（apt）==="
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y mosquitto mosquitto-clients
}

epel_major_for_system() {
    local rpm_major=""

    # Alibaba Cloud Linux 4 的 glibc 版本高于 EL9，使用 EPEL 9 包更稳妥。
    if [ "${ID:-}" = "alinux" ]; then
        case "${VERSION_ID:-}" in
            4*) echo "9"; return ;;
            3*) echo "8"; return ;;
        esac
    fi

    if command -v rpm >/dev/null 2>&1; then
        rpm_major="$(rpm -E '%{rhel}' 2>/dev/null || true)"
        if [[ "${rpm_major}" =~ ^[0-9]+$ ]]; then
            echo "${rpm_major}"
            return
        fi
    fi

    echo "${VERSION_ID%%.*}"
}

ensure_epel_repo() {
    local pm="$1"
    local epel_major="$2"
    local arch=""
    local epel_root="${EPEL_MIRROR:-https://mirrors.aliyun.com/epel}"
    local repo_file="/etc/yum.repos.d/whitebox-epel${epel_major}.repo"

    if command -v rpm >/dev/null 2>&1; then
        arch="$(rpm --eval '%{_arch}')"
    else
        arch="$(uname -m)"
    fi

    echo "=== 配置 EPEL ${epel_major}（阿里云镜像）==="
    rpm --import "${epel_root}/RPM-GPG-KEY-EPEL-${epel_major}" || true

    cat > "${repo_file}" <<EOF
[whitebox-epel${epel_major}]
name=Whitebox EPEL ${epel_major} for Mosquitto
baseurl=${epel_root}/${epel_major}/Everything/${arch}/
enabled=1
gpgcheck=1
gpgkey=${epel_root}/RPM-GPG-KEY-EPEL-${epel_major}
EOF

    "${pm}" makecache --disablerepo="*" --enablerepo="whitebox-epel${epel_major}" || true
}

install_with_rpm_pm() {
    local pm="$1"
    local epel_major=""

    echo "=== 安装 Mosquitto（${pm}）==="
    if "${pm}" install -y mosquitto; then
        return
    fi

    echo "系统默认源未提供 Mosquitto，准备启用 EPEL 镜像源。"
    epel_major="$(epel_major_for_system)"
    if [ -z "${epel_major}" ]; then
        echo "无法判断 EPEL 大版本，请检查 /etc/os-release。"
        exit 1
    fi

    if [ "${ID:-}" = "alinux" ] && [[ "${VERSION_ID:-}" == 4* ]]; then
        ensure_epel_repo "${pm}" "${epel_major}"
    elif ! "${pm}" install -y epel-release; then
        ensure_epel_repo "${pm}" "${epel_major}"
    fi

    "${pm}" install -y mosquitto
}

install_mosquitto() {
    if command -v dnf >/dev/null 2>&1; then
        install_with_rpm_pm dnf
    elif command -v yum >/dev/null 2>&1; then
        install_with_rpm_pm yum
    elif command -v apt-get >/dev/null 2>&1; then
        install_with_apt
    else
        echo "未找到 dnf/yum/apt-get，无法自动安装 Mosquitto。"
        exit 1
    fi
}

ensure_conf_dir_included() {
    local main_conf="/etc/mosquitto/mosquitto.conf"

    if [ -f "${main_conf}" ] && ! grep -Eq '^[[:space:]]*include_dir[[:space:]]+/etc/mosquitto/conf.d' "${main_conf}"; then
        printf '\ninclude_dir /etc/mosquitto/conf.d\n' >> "${main_conf}"
    fi
}

configure_mosquitto() {
    echo "=== 配置 Mosquitto ==="
    mkdir -p "${CONF_DIR}"

    cat > "${CONF_FILE}" <<EOF
listener 1883 0.0.0.0
allow_anonymous false
password_file ${PASSWD_FILE}
persistence true
EOF

    ensure_conf_dir_included
}

create_mqtt_user() {
    echo "=== 创建 MQTT 用户 ==="
    if ! command -v mosquitto_passwd >/dev/null 2>&1; then
        echo "未找到 mosquitto_passwd，请确认 Mosquitto 已正确安装。"
        exit 1
    fi

    mosquitto_passwd -c -b "${PASSWD_FILE}" "${MQTT_USER}" "${MQTT_PASS}"
    chmod 640 "${PASSWD_FILE}" || true
    chown root:mosquitto "${PASSWD_FILE}" 2>/dev/null || true
}

start_mosquitto() {
    echo "=== 启动 Mosquitto ==="
    if command -v systemctl >/dev/null 2>&1; then
        systemctl enable mosquitto
        systemctl restart mosquitto
        if ! systemctl is-active --quiet mosquitto; then
            echo "Mosquitto 启动失败，最近日志如下："
            journalctl -u mosquitto -n 50 --no-pager || true
            exit 1
        fi
    else
        service mosquitto restart
    fi
}

print_result() {
    local server_ip=""

    server_ip="$(hostname -I 2>/dev/null || true)"
    server_ip="${server_ip%% *}"
    if [ -z "${server_ip}" ]; then
        server_ip="$(hostname 2>/dev/null || echo "服务器IP")"
    fi

    echo ""
    echo "Done! MQTT: ${server_ip}:1883  user=${MQTT_USER}"
    echo "密码: ${MQTT_PASS}"
}

main() {
    require_root
    detect_system
    install_mosquitto
    configure_mosquitto
    create_mqtt_user
    start_mosquitto
    print_result
}

main "$@"
