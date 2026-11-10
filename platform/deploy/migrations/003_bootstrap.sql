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

-- 老部署（2026-11 之前）的企业主体叫旧品牌名。on conflict do nothing 不会改它，
-- 于是重装后顶栏和导出文件里还是旧名。这里只在「名字仍是旧品牌」时纠正，
-- 客户自己改过名的不动 —— 这条语句必须幂等，每次安装都会重跑。
update forge.enterprises
   set name = '智途', short_name = '智途'
 where id = 'ent_forge'
   and name in ('石研博航', '石研博航（演示）');
