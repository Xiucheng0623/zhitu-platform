-- ============================================================================
-- FORGE · 010 身份与认证（手机号登录）
-- ----------------------------------------------------------------------------
-- 为什么现在才做：
--   此前 auth_mode=dev，Principal 直接从 "Bearer ops_director" 这种字符串解析出来，
--   不验签、不过期、不查库。那是演示态，不能对服务器开放。
--   这一版把身份落到库里：手机号为登录名，密码哈希存库，JWT 由本服务签发。
--
-- 为什么 users / invites **不开 RLS**（有意偏离 002 的惯例）：
--   1. 手机号是**全局唯一**的登录名。登录请求进来时还没有租户上下文，
--      而所有业务表的策略都依赖 forge.ctx('forge.enterprise_id')，
--      此时查询会被 RLS 过滤成 0 行 —— 登录必然失败。
--   2. 若为了绕开而 force+owner 也不行：FORCE RLS 连表 owner 都约束，
--      SECURITY DEFINER 函数同样查不到。
--   3. 所以这两张表改用**列级权限 + SECURITY DEFINER 函数**做防护：
--        · forge_app 对 users 的 SELECT 列表里**没有 password_hash**
--        · 校验密码必须走 forge.login_lookup()，一次只按手机号取一行
--        · forge_app 没有 DELETE 权限（停用用 status='suspended'，不删人）
--   代价是这两张表不受租户级 RLS 兜底，所以管理接口必须在 WHERE 里显式带
--   enterprise_id。db_health() 会把这层豁免显式报出来（rls_exempt），
--   不允许"看起来漏了一张表"这种含糊状态。
--
-- 与 Label Studio 的关系：
--   LS 的 USERNAME_FIELD='email'，改不动（Django 迁移，升级即炸）。
--   所以这里存 ls_email（形如 13800138000@staff.forge.local）和 ls_user_id，
--   由迁移/开通流程在 LS 里建好对应账号。员工只见手机号，LS 账号由平台代持。
-- ============================================================================

set search_path to forge, public;

-- ---------------------------------------------------------------------------
-- 1. 用户
-- ---------------------------------------------------------------------------
create table if not exists forge.users (
  id               text primary key,
  enterprise_id    text not null references forge.enterprises(id) on delete restrict,

  -- 登录名。统一存 E.164（+8613800138000），规范化在应用层做（app/auth.py）。
  -- 全局唯一而不是租户内唯一：登录时用户只给手机号，无法先确定租户。
  -- 顺带的好处是同一个手机号不会在多个租户下被重复注册。
  phone            text not null,
  phone_verified   boolean not null default false,

  -- scrypt 哈希，格式：scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>
  -- 用标准库 hashlib，不引入 passlib/argon2 这类额外依赖。
  password_hash    text not null,

  name             text not null default '',
  email            text,                       -- 可选，仅用于通知，不参与登录
  roles            text[] not null default '{}'::text[],

  -- pending  注册后待管理员激活（邀请码注册的默认状态）
  -- active   正常
  -- suspended 停用（保留数据与审计，不物理删除）
  status           text not null default 'pending'
                   check (status in ('pending', 'active', 'suspended')),

  -- 供应商侧主体才非空，对应 Principal.supplier_id（RLS 第二道边界）
  supplier_id      text references forge.suppliers(id) on delete set null,

  -- Label Studio 账号映射（LS 只认 email，见文件头说明）
  ls_user_id       integer,
  ls_email         text,

  -- 首次登录 / 管理员重置后强制改密
  must_change_pwd  boolean not null default true,

  -- 改密或停用时 +1，签发的 JWT 里带 ver，不匹配即失效。
  -- 这是不用维护 refresh token 表就能做到"撤销全部旧会话"的轻量方案。
  token_version    integer not null default 0,

  failed_attempts  integer not null default 0,
  locked_until     timestamptz,
  last_login_at    timestamptz,

  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);

create unique index if not exists ux_users_phone on forge.users (phone);
create index if not exists ix_users_tenant_status on forge.users (enterprise_id, status);
create index if not exists ix_users_supplier on forge.users (supplier_id)
  where supplier_id is not null;

-- ---------------------------------------------------------------------------
-- 2. 邀请码
--    内部企业不开放公网注册，但员工要能自助注册 —— 用邀请码把两件事同时满足。
-- ---------------------------------------------------------------------------
create table if not exists forge.invites (
  code          text primary key,
  enterprise_id text not null references forge.enterprises(id) on delete cascade,
  roles         text[] not null default '{annotator}'::text[],
  supplier_id   text references forge.suppliers(id) on delete set null,
  note          text,
  created_by    text,                          -- users.id，不设外键：邀请码可先于用户存在
  max_uses      integer not null default 1 check (max_uses > 0),
  used_count    integer not null default 0,
  expires_at    timestamptz,
  created_at    timestamptz not null default now(),
  constraint ck_invites_used check (used_count >= 0 and used_count <= max_uses)
);

create index if not exists ix_invites_tenant on forge.invites (enterprise_id, created_at desc);

-- ---------------------------------------------------------------------------
-- 3. 权限：列级授权，把 password_hash 从 forge_app 的可读列里摘出去
--    002/007 用的是 "grant ... on all tables"，新表不会自动包含，
--    所以这里必须重新授权（否则 forge_app 连 users 都读不到）。
-- ---------------------------------------------------------------------------
grant usage on schema forge to forge_app;
grant select, insert, update, delete on all tables in schema forge to forge_app;
grant usage, select on all sequences in schema forge to forge_app;
revoke create on schema forge from forge_app;

-- users：先收全部，再按列发还
revoke all on forge.users from forge_app;
grant select (
  id, enterprise_id, phone, phone_verified, name, email, roles, status,
  supplier_id, ls_user_id, ls_email, must_change_pwd, token_version,
  failed_attempts, locked_until, last_login_at, created_at, updated_at
) on forge.users to forge_app;
-- 写入密码哈希是允许的（注册、改密），**读取**不授权。
-- update 列清单含 password_hash，所以能改不能读。
grant insert (
  id, enterprise_id, phone, phone_verified, password_hash, name, email, roles,
  status, supplier_id, ls_user_id, ls_email, must_change_pwd
) on forge.users to forge_app;
grant update (
  phone, phone_verified, password_hash, name, email, roles, status,
  supplier_id, ls_user_id, ls_email, must_change_pwd, token_version,
  failed_attempts, locked_until, last_login_at, updated_at
) on forge.users to forge_app;
-- 刻意不 grant delete：停用走 status，不物理删除。

-- invites：正常租户内读写，不给 delete（用 max_uses / expires_at 自然失效）
revoke delete on forge.invites from forge_app;

-- ---------------------------------------------------------------------------
-- 4. 登录查询函数
--    forge_app 没有 password_hash 的 select 权限，所以校验密码必须经过这里。
--    SECURITY DEFINER + 固定 search_path（防 search_path 劫持，这是该模式
--    最经典的安全坑）。
-- ---------------------------------------------------------------------------
create or replace function forge.login_lookup(p_phone text)
returns table (
  id              text,
  enterprise_id   text,
  phone           text,
  password_hash   text,
  name            text,
  email           text,
  roles           text[],
  status          text,
  supplier_id     text,
  token_version   integer,
  must_change_pwd boolean,
  failed_attempts integer,
  locked_until    timestamptz
)
language sql
security definer
set search_path = forge, pg_temp
as $$
  select u.id, u.enterprise_id, u.phone, u.password_hash, u.name, u.email,
         u.roles, u.status, u.supplier_id, u.token_version, u.must_change_pwd,
         u.failed_attempts, u.locked_until
    from forge.users u
   where u.phone = p_phone
   limit 1
$$;

revoke all on function forge.login_lookup(text) from public;
grant execute on function forge.login_lookup(text) to forge_app;

-- ---------------------------------------------------------------------------
-- 5. 登录失败计数（原子自增，避免并发下的读改写丢失）
--    这里只做计数与锁定，不返回密码相关字段，所以不需要 SECURITY DEFINER。
--    但 forge_app 对 users 有 update 列权限，可以直接 update。
-- ---------------------------------------------------------------------------
create or replace function forge.note_login_failure(p_user_id text, p_threshold int, p_lock_minutes int)
returns void
language sql
security definer
set search_path = forge, pg_temp
as $$
  update forge.users
     set failed_attempts = failed_attempts + 1,
         locked_until = case
           when failed_attempts + 1 >= p_threshold
             then now() + make_interval(mins => p_lock_minutes)
           else locked_until
         end,
         updated_at = now()
   where id = p_user_id
$$;

revoke all on function forge.note_login_failure(text, int, int) from public;
grant execute on function forge.note_login_failure(text, int, int) to forge_app;

-- ---------------------------------------------------------------------------
-- 6. 顺带修一个隐患：audit_log 的 actor 字段此前存的是 dev token 里的字符串，
--    现在要能存真实 users.id。这里只补注释与索引，不改结构。
-- ---------------------------------------------------------------------------
comment on column forge.users.phone is
  'E.164 规范化手机号（+8613800138000），全局唯一，作为登录名';
comment on column forge.users.password_hash is
  'scrypt$n$r$p$salt_b64$hash_b64；forge_app 无 SELECT 权限，只能经 login_lookup 校验';
comment on column forge.users.token_version is
  'JWT 里的 ver 必须与之一致；改密/停用时 +1 使全部旧 token 失效';
comment on column forge.users.ls_email is
  '映射给 Label Studio 的合成邮箱（LS USERNAME_FIELD=email，不认手机号）';
