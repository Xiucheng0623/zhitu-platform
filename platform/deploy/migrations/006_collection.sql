-- ============================================================================
-- FORGE · 006 数据采集域
-- ----------------------------------------------------------------------------
-- 为什么需要独立一域：
--   海天瑞声这类专业数据服务商的收入结构里，采集（collection）和标注
--   （annotation）是两条并列的产线，不是同一条流程的两个阶段。
--   采集有其独有的对象：采集方案、采集脚本、采集现场、采集员、采集场次。
--   把采集硬塞进 projects/tasks 里，会让"任务"同时表示标注批次和采集场次，
--   状态机互相污染 —— 这是想省钱反而最贵的做法。
--
-- 与标注域的边界：
--   采集产出「原始数据」（进 assets，status=raw）；
--   标注消费这些原始数据。两者通过 project_id 关联，不共享状态机。
-- ============================================================================

set search_path to forge, public;

-- ---------------------------------------------------------------------------
-- 1. 采集员
-- ---------------------------------------------------------------------------
create table if not exists forge.collectors (
  id                text primary key,
  enterprise_id     text not null references forge.enterprises(id) on delete restrict,
  -- 采集员可能直属于平台（自建采集基地），也可能由供应商组织。
  -- 可空 = 平台自有。
  supplier_id       text references forge.suppliers(id) on delete set null,
  code              text not null,
  name              text not null,
  initials          text,
  gender            text check (gender in ('male','female','other')),
  birth_year        integer check (birth_year between 1900 and 2100),
  phone             text,
  city              text,
  -- 口音 / 方言标签，语音采集的核心筛选维度
  accents           text[] not null default '{}',
  -- 擅长的采集类型
  skills            text[] not null default '{}',
  status            text not null default 'screening'
                    check (status in ('screening','active','paused','blacklisted')),
  sessions_done     integer not null default 0 check (sessions_done >= 0),
  valid_clips       integer not null default 0 check (valid_clips >= 0),
  quality_score     numeric(4,1) not null default 0 check (quality_score between 0 and 100),
  id_verified       boolean not null default false,
  agreement_signed  boolean not null default false,
  joined_at         timestamptz not null default now(),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  -- 未实名 / 未签协议的人不能上岗，这是数据合规的硬门槛（个人信息保护法）
  constraint ck_collector_can_work
    check (status <> 'active' or (id_verified and agreement_signed))
);
create unique index if not exists ux_collectors_code
  on forge.collectors (enterprise_id, code);
create index if not exists ix_collectors_status
  on forge.collectors (enterprise_id, status);
create index if not exists ix_collectors_supplier
  on forge.collectors (supplier_id, status);

-- ---------------------------------------------------------------------------
-- 2. 采集任务（一个采集方案 = 一次采集任务的配置）
-- ---------------------------------------------------------------------------
create table if not exists forge.collection_tasks (
  id                  text primary key,
  enterprise_id       text not null references forge.enterprises(id) on delete restrict,
  project_id          text not null references forge.projects(id) on delete cascade,
  code                text not null,
  name                text not null,
  modality            text not null check (modality in ('audio','image','video','text','multimodal')),
  -- 采集场景：车内 / 安静室内 / 嘈杂街道 / 户外风噪 ... 直接影响单价
  scene               text,
  -- 设备与参数要求，例如 "44.1kHz/16bit 单声道 / 领夹麦"
  device_requirement  text,
  environment         text,
  script_type         text not null default 'read'
                      check (script_type in ('read','spontaneous','dialogue','keyword','guided')),
  target_sessions     integer not null default 0 check (target_sessions >= 0),
  target_valid_clips  integer not null default 0 check (target_valid_clips >= 0),
  target_duration_min integer not null default 0 check (target_duration_min >= 0),
  done_sessions       integer not null default 0 check (done_sessions >= 0),
  valid_clips         integer not null default 0 check (valid_clips >= 0),
  status              text not null default 'draft'
                      check (status in ('draft','recruiting','running','paused','completed','cancelled')),
  -- 每个有效片段的价格，采集多按片段结算（不同于标注按条）
  unit_price          numeric(10,4) not null default 0 check (unit_price >= 0),
  budget              numeric(14,2) not null default 0 check (budget >= 0),
  spent               numeric(14,2) not null default 0 check (spent >= 0),
  start_at            timestamptz,
  due_at              timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
create unique index if not exists ux_ct_code
  on forge.collection_tasks (enterprise_id, code);
create index if not exists ix_ct_tenant_status
  on forge.collection_tasks (enterprise_id, status);
create index if not exists ix_ct_project
  on forge.collection_tasks (project_id, status);

-- ---------------------------------------------------------------------------
-- 3. 采集脚本（要念的文本 / 要做的动作）
-- ---------------------------------------------------------------------------
create table if not exists forge.collection_scripts (
  id            bigserial primary key,
  enterprise_id text not null,
  task_id       text not null references forge.collection_tasks(id) on delete cascade,
  seq           integer not null check (seq > 0),
  content       text not null check (length(btrim(content)) > 0),
  category      text,
  difficulty    text not null default 'normal' check (difficulty in ('easy','normal','hard')),
  -- 必录项：漏录会导致整场不合格
  required      boolean not null default true,
  created_at    timestamptz not null default now()
);
create unique index if not exists ux_script_task_seq
  on forge.collection_scripts (task_id, seq);

-- ---------------------------------------------------------------------------
-- 4. 采集场次（一位采集员在一个现场完成的一次采集）
-- ---------------------------------------------------------------------------
create table if not exists forge.collection_sessions (
  id                  text primary key,
  enterprise_id       text not null references forge.enterprises(id) on delete restrict,
  task_id             text not null references forge.collection_tasks(id) on delete cascade,
  collector_id        text not null references forge.collectors(id) on delete restrict,
  site                text,
  city                text,
  room                text,
  device              text,
  sample_rate         integer check (sample_rate is null or sample_rate > 0),
  channels            integer check (channels is null or channels > 0),
  started_at          timestamptz,
  ended_at            timestamptz,
  duration_min        integer not null default 0 check (duration_min >= 0),
  clip_count          integer not null default 0 check (clip_count >= 0),
  valid_clip_count    integer not null default 0 check (valid_clip_count >= 0),
  rejected_clip_count integer not null default 0 check (rejected_clip_count >= 0),
  qc_status           text not null default 'pending'
                      check (qc_status in ('pending','passed','partial','failed')),
  qc_note             text,
  status              text not null default 'scheduled'
                      check (status in ('scheduled','recording','submitted','qc','accepted','rejected')),
  payout              numeric(14,2) not null default 0 check (payout >= 0),
  -- MinIO 前缀，原始音频/图像都在这下面
  object_prefix       text,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  constraint ck_session_clip_math
    check (valid_clip_count + rejected_clip_count <= clip_count)
);
create index if not exists ix_cs_task
  on forge.collection_sessions (task_id, status);
create index if not exists ix_cs_collector
  on forge.collection_sessions (collector_id, started_at desc);
create index if not exists ix_cs_tenant_time
  on forge.collection_sessions (enterprise_id, started_at desc);
-- 一位采集员同一时间只能有一场进行中的采集，否则片段归属会混淆
create unique index if not exists ux_cs_collector_active
  on forge.collection_sessions (collector_id)
  where status in ('scheduled','recording');

-- ---------------------------------------------------------------------------
-- 5. 触发器：场次落定后回写任务完成量与采集员累计量
-- ---------------------------------------------------------------------------
create or replace function forge.sync_collection_progress(p_task_id text)
returns void as $$
declare
  v_sessions integer;
  v_valid    integer;
  v_spent    numeric(14,2);
begin
  select count(*) filter (where status = 'accepted'),
         coalesce(sum(valid_clip_count) filter (where status = 'accepted'), 0),
         coalesce(sum(payout) filter (where status = 'accepted'), 0)
    into v_sessions, v_valid, v_spent
    from forge.collection_sessions
   where task_id = p_task_id;

  update forge.collection_tasks
     set done_sessions = v_sessions,
         valid_clips   = v_valid,
         spent         = v_spent,
         updated_at    = now()
   where id = p_task_id;
end;
$$ language plpgsql;

create or replace function forge.trg_sync_collection_progress() returns trigger as $$
begin
  if tg_op = 'DELETE' then
    perform forge.sync_collection_progress(old.task_id);
    return old;
  end if;
  perform forge.sync_collection_progress(new.task_id);
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_cs_sync on forge.collection_sessions;
create trigger trg_cs_sync after insert or update or delete on forge.collection_sessions
  for each row execute function forge.trg_sync_collection_progress();

-- 采集员累计通过场次
create or replace function forge.trg_sync_collector_stats() returns trigger as $$
declare
  v_clips integer;
  v_done  integer;
begin
  select count(*) filter (where status = 'accepted'),
         coalesce(sum(valid_clip_count) filter (where status = 'accepted'), 0)
    into v_done, v_clips
    from forge.collection_sessions
   where collector_id = new.collector_id;

  update forge.collectors
     set sessions_done = v_done,
         valid_clips   = v_clips,
         updated_at    = now()
   where id = new.collector_id;
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_cs_collector on forge.collection_sessions;
create trigger trg_cs_collector after insert or update of status on forge.collection_sessions
  for each row execute function forge.trg_sync_collector_stats();

do $$
declare t text;
begin
  foreach t in array array['collectors','collection_tasks','collection_sessions']
  loop
    execute format('drop trigger if exists trg_touch_%1$s on forge.%1$s', t);
    execute format('create trigger trg_touch_%1$s before update on forge.%1$s
                    for each row execute function forge.touch_updated_at()', t);
  end loop;
end $$;

-- ---------------------------------------------------------------------------
-- 6. 视图：采集进度
-- ---------------------------------------------------------------------------
create or replace view forge.v_collection_progress as
select
  t.enterprise_id,
  t.id                as task_id,
  t.project_id,
  t.code,
  t.name,
  t.modality,
  t.scene,
  t.status,
  t.target_sessions,
  t.done_sessions,
  t.target_valid_clips,
  t.valid_clips,
  round(t.done_sessions::numeric / nullif(t.target_sessions, 0) * 100, 2) as session_pct,
  round(t.valid_clips::numeric / nullif(t.target_valid_clips, 0) * 100, 2)    as clip_pct,
  t.budget,
  t.spent,
  (select count(*) from forge.collection_sessions s
    where s.task_id = t.id and s.status in ('submitted','qc'))                as pending_sessions,
  (select count(*) from forge.collection_sessions s
    where s.task_id = t.id and s.qc_status = 'failed')                        as failed_sessions,
  t.due_at
from forge.collection_tasks t;
