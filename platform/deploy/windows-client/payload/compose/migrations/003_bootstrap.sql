-- ============================================================================
-- FORGE · 003 部署引导（幂等，无演示数据）
-- ----------------------------------------------------------------------------
-- 只做一件事：保证默认企业主体存在。
--
-- 为什么不复用原来的 003_seed_verify.sql：
--   那份脚本灌的是**隔离性验收数据**（两个租户、假供应商、假项目、假结算单），
--   用于证明 RLS 边界成立。它属于测试夹具，不是部署内容 ——
--   在生产库上跑一次，客户第一眼看到的就是一屏幕虚构公司与金额。
--   现在统一挪到 migrations/verification/，需要做 RLS 验收时显式执行。
--
-- 管理员账号不在这建：密码哈希要走 orchestrator 的 scrypt（app/auth.py），
-- 由 install.sh 调 `python -m app.cli create-admin` 创建，避免 SQL 里出现口令。
-- ============================================================================

set search_path to forge, public;

insert into forge.enterprises (id, name, short_name, plan, seats)
values ('ent_forge', '智途', '智途', 'internal', 50)
on conflict (id) do nothing;
