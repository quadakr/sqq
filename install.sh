#!/bin/sh
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root (sudo ./install.sh)"
    exit 1
fi

echo "Checking dependencies..."

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: python3 is not installed."
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "Error: curl is not installed."
    exit 1
fi

echo "Installing shellqq..."
rm -rf /usr/local/bin/shellqq
curl -fsSL https://raw.githubusercontent.com/quadakr/sqq/main/sqq.py \
    -o /usr/local/bin/shellqq
chmod +x /usr/local/bin/shellqq

echo "Linking sqq -> shellqq..."
rm -f /usr/local/bin/sqq
ln -s /usr/local/bin/shellqq /usr/local/bin/sqq

echo "shellqq installed to /usr/local/bin/shellqq"
echo "Short alias 'sqq' installed to /usr/local/bin/sqq"
echo "Run: shellqq (or: sqq)"
