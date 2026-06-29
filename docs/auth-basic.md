# Nginx Basic Auth

This project exposes one internal access port:

```text
http://服务器IP:55589
```

Nginx protects both the frontend and `/api/` with Basic Auth. The backend `8000` and frontend `3000` ports are only exposed inside the Docker network.

## Prepare Local Files

`dashboard/config.json`, `dashboard/data/`, and `nginx/.htpasswd` are local runtime files. Do not commit `dashboard/config.json` or `nginx/.htpasswd`.

```bash
cp -n dashboard/config.example.json dashboard/config.json
mkdir -p dashboard/data
mkdir -p nginx
```

## Generate Password File

Do not commit `nginx/.htpasswd`.

```bash
sudo apt update
sudo apt install -y apache2-utils
htpasswd -c nginx/.htpasswd admin
chmod 644 nginx/.htpasswd
```

## Start

```bash
cp -n dashboard/config.example.json dashboard/config.json
mkdir -p dashboard/data
mkdir -p nginx
htpasswd -c nginx/.htpasswd admin
chmod 644 nginx/.htpasswd
docker compose down --remove-orphans
docker compose up -d --build
```

## Access

Open:

```text
http://服务器IP:55589
```

Enter the username and password created by `htpasswd`.

## Change Password

```bash
htpasswd nginx/.htpasswd admin
chmod 644 nginx/.htpasswd
docker compose restart nginx
```

Browsers may cache Basic Auth credentials. If the old login appears to persist, close the tab or use a private window.
