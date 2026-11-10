-- ============================================================================
-- FORGE · 000 角色与权限基线
-- ----------------------------------------------------------------------------
-- 为什么单独一个文件：
--   建角色必须在业务迁移之前，且密码来自部署环境，不能硬编码。
--   用 \gexec 让 psql 按需执行（create 幂等 + 密码可轮换）。
--
-- 执行方式（migrate 容器内）：
--   psql -v ON_ERROR_STOP=1 -v app_password="$APP_PASSWORD" -f 000_roles.sql
--
-- 关键点：forge_app 是「应用账号」，不是 owner，也不给 superuser / bypassrls。
-- RLS 对 owner 默认不生效，若图省事让应用用 owner 连接，整套隔离会静默失效。
-- ============================================================================

\set ON_ERROR_STOP on

select format('create role forge_app login password %L', :'app_password')
 where not exists (select 1 from pg_roles where rolname = 'forge_app')
\gexec

alter role forge_app with login password :'app_password';

-- 明确收紧：任何情况下都不允许绕行 RLS
alter role forge_app nosuperuser nobypassrls nocreatedb nocreaterole;

-- 连接数上限，防止中间层把库连接打爆
alter role forge_app connection limit 60;
