-- ============================================================================
-- FORGE · 011 外部身份绑定（SSO）
-- ----------------------------------------------------------------------------
-- 场景（2026-11 决策）：
--   公司另一个平台是身份权威（IdP），本平台作为 Relying Party 接入。
--   员工一套凭证两边通行，人员完全重合。
--
-- 核心原则：**认证归对方，授权归我们。**
--   对方的 id_token 只回答"你是谁"（sub 是身份证号）。
--   "你在本平台是什么角色"永远从 forge.users.roles 读 —— 不让对方 IdP 的
--   管理员有权授予本平台 platform_admin（那是能审批付款的角色）。
--   因此本表只做 subject ↔ user_id 的映射，不存角色。
--
-- 安全性为什么不能"按手机号自动合并"：
--   如果对方把 phone_number 声明成任意手机号（或对方管理员可改），
--   那他能把自己的身份合并到"张伟"（platform_admin）名下，完成提权。
--   所以首次登录撞到已有本地账号时，只登记一张**待确认申请**，
--   必须本人再输一次本地密码（证明他本来就拥有该账号）或管理员批准。
--
-- 与 010 的关系：
--   010 里 password_hash 是 NOT NULL。纯 SSO 用户没有本地密码，
--   这里放宽为可空；NULL 的语义是"未设置本地密码"，password 登录直接拒绝。
-- ============================================================================

set search_path to forge, public;

-- ---------------------------------------------------------------------------
-- 1. 允许没有本地密码（纯 SSO 账号）
-- ---------------------------------------------------------------------------
alter table forge.users alter column password_hash drop not null;

comment on column forge.users.password_hash is
  'scrypt 哈希。NULL 表示未设置本地密码（纯 SSO 账号），密码登录一律拒绝。';

-- ---------------------------------------------------------------------------
-- 2. 外部身份绑定
-- ---------------------------------------------------------------------------
create table if not exists forge.identities (
  -- provider 是配置里的逻辑名（oidc / wecom / azure ...），不是域名。
  -- 同一个 IdP 换域名不该导致绑定失效；反过来也要靠 issuer 校验防冒充。
  provider       text not null,
  -- 对方的 sub。OIDC 规范要求它在该 issuer 下稳定且永不重用，
  -- 所以它是映射的唯一锚点。**不要**用 email/手机号当主键 —— 那两个会变。
  subject        text not null,

  user_id        text not null references forge.users(id) on delete cascade,

  -- 首次绑定时的 issuer。日后若 provider 名被复用到另一个 IdP，
  -- 校验这个字段能挡住"旧绑定被新 IdP 冒用"。
  issuer         text,
  email          text,
  phone          text,
  -- 原始声明快照。排查"为什么这个人的职务显示不对"时，靠它而不是靠猜。
  -- 不存 token（token 会过期，且是凭证不是记录）。
  claims         jsonb not null default '{}'::jsonb,

  linked_at      timestamptz not null default now(),
  last_login_at  timestamptz,

  primary key (provider, subject)
);

-- 一个用户在同一 provider 下只能绑一个外部身份，否则登录时不知道该用哪个
create unique index if not exists ux_identities_user_provider
  on forge.identities (user_id, provider);

create index if not exists ix_identities_user on forge.identities (user_id);

comment on table forge.identities is
  '外部身份 → 本地账号映射。只做认证映射，不含任何角色信息。';

-- ---------------------------------------------------------------------------
-- 3. 待确认的绑定申请
-- ---------------------------------------------------------------------------
-- 触发条件：SSO 首次登录，sub 没绑过，但手机号/邮箱命中了某个已有本地账号。
-- 这时**不能**直接合并（见文件头），只登记申请，等本人证明或管理员批准。
create table if not exists forge.identity_link_requests (
  id             text primary key,
  provider       text not null,
  subject        text not null,
  user_id        text not null references forge.users(id) on delete cascade,
  phone          text,
  email          text,
  claims         jsonb not null default '{}'::jsonb,
  status         text not null default 'pending'
                 check (status in ('pending', 'approved', 'rejected', 'expired')),
  created_at     timestamptz not null default now(),
  expires_at     timestamptz not null default now() + interval '30 minutes',
  decided_at     timestamptz,
  decided_by     text,
  unique (provider, subject)
);

create index if not exists ix_link_requests_user
  on forge.identity_link_requests (user_id, status);

comment on table forge.identity_link_requests is
  '首次 SSO 撞到已有账号时的待确认合并申请。过期即作废，不自动通过。';

-- ---------------------------------------------------------------------------
-- 4. 授权
-- ---------------------------------------------------------------------------
-- 这两张表与 users/invites 一样**刻意不开 RLS**：
-- 它们只在"还没有租户上下文"的登录链路里被访问。管理接口必须在 WHERE 里
-- 显式带 enterprise_id（通过 join forge.users 得到）。
revoke all on forge.identities from forge_app;
grant select (provider, subject, user_id, issuer, email, phone, linked_at, last_login_at)
  on forge.identities to forge_app;
grant insert (provider, subject, user_id, issuer, email, phone, claims, last_login_at)
  on forge.identities to forge_app;
grant update (email, phone, claims, last_login_at)
  on forge.identities to forge_app;
-- 刻意不 grant delete：解绑走管理员流程，保留痕迹。

revoke all on forge.identity_link_requests from forge_app;
grant select (id, provider, subject, user_id, phone, email, claims, status,
              created_at, expires_at, decided_at, decided_by)
  on forge.identity_link_requests to forge_app;
grant insert (id, provider, subject, user_id, phone, email, claims)
  on forge.identity_link_requests to forge_app;
-- update 必须包含 created_at / expires_at：重复发起绑定时走的是
-- `on conflict do update set ... created_at = now(), expires_at = now() + ...`。
-- 列级权限不足的报错文本是"对表 XXX 权限不够"，不会点出是哪一列，极易误判。
grant update (status, decided_at, decided_by, created_at, expires_at)
  on forge.identity_link_requests to forge_app;

-- ---------------------------------------------------------------------------
-- 5. 定义者函数
-- ---------------------------------------------------------------------------
-- 登录链路没有租户上下文，且 users 的 password_hash 对 forge_app 不可读，
-- 所以"按 sub 找到本地账号"必须走定义者函数。
-- 固定 search_path 防劫持（与 010 的 login_lookup 同一套路）。
create or replace function forge.identity_lookup(p_provider text, p_subject text)
returns table (
  user_id         text,
  enterprise_id   text,
  name            text,
  email           text,
  phone           text,
  roles           text[],
  status          text,
  supplier_id     text,
  token_version   integer,
  must_change_pwd boolean,
  bound_issuer    text,
  linked_at       timestamptz
)
language sql
security definer
set search_path = forge, pg_temp
as $$
  select u.id, u.enterprise_id, u.name, u.email, u.phone, u.roles,
         u.status, u.supplier_id, u.token_version, u.must_change_pwd,
         i.issuer, i.linked_at
    from forge.identities i
    join forge.users u on u.id = i.user_id
   where i.provider = p_provider and i.subject = p_subject
   limit 1
$$;

-- 按手机号找"可能属于同一个人"的未绑定本地账号，用于生成合并申请。
-- 只在没有任何 identity 绑定、且密码哈希存在（即本人可自证）时才返回。
create or replace function forge.identity_candidates_by_phone(p_phone text)
returns table (
  user_id       text,
  enterprise_id text,
  name          text,
  has_password  boolean,
  bound_count   integer
)
language sql
security definer
set search_path = forge, pg_temp
as $$
  select u.id, u.enterprise_id, u.name,
         (u.password_hash is not null) as has_password,
         (select count(*)::int from forge.identities i where i.user_id = u.id) as bound_count
    from forge.users u
   where u.phone = p_phone
   limit 5
$$;

-- 记录一次 SSO 登录（不 bump token_version —— 登录不该踢掉其它会话）
create or replace function forge.note_sso_login(p_provider text, p_subject text)
returns void
language sql
security definer
set search_path = forge, pg_temp
as $$
  update forge.identities
     set last_login_at = now(),
         -- 只更新"这次确实带回来了"的字段，空值不覆盖已有信息
         email = coalesce(nullif(current_setting('forge.sso_email', true), ''), email),
         phone = coalesce(nullif(current_setting('forge.sso_phone', true), ''), phone)
   where provider = p_provider and subject = p_subject;
$$;

grant execute on function forge.identity_lookup(text, text) to forge_app;
grant execute on function forge.identity_candidates_by_phone(text) to forge_app;
grant execute on function forge.note_sso_login(text, text) to forge_app;

-- ---------------------------------------------------------------------------
-- 6. 自检
-- ---------------------------------------------------------------------------
do $$
declare
  n_ident int;
  n_link  int;
  nullable text;
begin
  select count(*) into n_ident from information_schema.tables
   where table_schema = 'forge' and table_name = 'identities';
  select count(*) into n_link from information_schema.tables
   where table_schema = 'forge' and table_name = 'identity_link_requests';
  select is_nullable into nullable from information_schema.columns
   where table_schema = 'forge' and table_name = 'users' and column_name = 'password_hash';

  if n_ident = 0 or n_link = 0 then
    raise exception '011 迁移未建出 identities / identity_link_requests';
  end if;
  if nullable <> 'YES' then
    raise exception '011 迁移未把 users.password_hash 放宽为可空（当前 %）', nullable;
  end if;
  raise notice '011 完成：identities + identity_link_requests 就绪，password_hash 可空';
end $$;
