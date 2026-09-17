#!/usr/bin/env bash
# 修复/固化 UserLoop 独立 MySQL 实例的凭据（仅影响 :3307 独立实例，不碰主库）
set -euo pipefail
BASE=/www/server/userloop-mysql
MYSQLD=/www/server/mysql/bin/mysqld
MYSQL=/www/server/mysql/bin/mysql

PW=$(head -c 18 /dev/urandom | base64 | tr -d '=+/' | head -c 20)
ROOTPW=$(head -c 18 /dev/urandom | base64 | tr -d '=+/' | head -c 20)

systemctl stop userloop-mysql || true
sleep 2

nohup "$MYSQLD" --defaults-file="$BASE/my.cnf" --skip-grant-tables >/tmp/ul-mysql-skip.log 2>&1 &
for _ in $(seq 1 25); do
  "$MYSQL" --socket="$BASE/run/mysql.sock" -uroot -e "SELECT 1" >/dev/null 2>&1 && break
  sleep 1
done

"$MYSQL" --socket="$BASE/run/mysql.sock" -uroot <<SQL
FLUSH PRIVILEGES;
ALTER USER 'root'@'localhost' IDENTIFIED BY '${ROOTPW}';
CREATE DATABASE IF NOT EXISTS userloop DEFAULT CHARSET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE USER IF NOT EXISTS 'userloop'@'127.0.0.1' IDENTIFIED BY '${PW}';
ALTER USER 'userloop'@'127.0.0.1' IDENTIFIED BY '${PW}';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, ALTER ON userloop.* TO 'userloop'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

"$MYSQL" --socket="$BASE/run/mysql.sock" -uroot -p"$ROOTPW" -e "SHUTDOWN;" || pkill -f "skip-grant-tables" || true
sleep 3
systemctl start userloop-mysql
sleep 5
systemctl is-active userloop-mysql

echo "--- 验证 root ---"
"$MYSQL" --socket="$BASE/run/mysql.sock" -uroot -p"$ROOTPW" -e "SHOW DATABASES;" | tr '\n' ' '
echo
echo "--- 验证 userloop 业务用户 ---"
"$MYSQL" -h127.0.0.1 -P3307 -uuserloop -p"$PW" userloop -e "SELECT DATABASE() AS db, CURRENT_USER() AS who;" | tr '\n' ' '
echo

printf 'root=%s\nuserloop=%s\n' "$ROOTPW" "$PW" > "$BASE/credentials.txt"
chmod 600 "$BASE/credentials.txt"
printf '%s' "$PW" > /tmp/ul_mysql_pw.txt
chmod 600 /tmp/ul_mysql_pw.txt
echo "凭据已写入 $BASE/credentials.txt (600) 与 /tmp/ul_mysql_pw.txt"
