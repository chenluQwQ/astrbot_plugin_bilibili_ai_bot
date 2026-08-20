-- BiliBot 统一存储层 schema。
-- 设计原则：一个事实只有一处真相；所有大文本可截断、可过期、可按用户删除。
-- 所有时间戳统一为 UTC 秒（REAL），避免本地时区在跨午夜逻辑里出错。

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------- 适配层
-- 统一入站事件表。唯一索引保证同一来源事件永不重复处理；
-- claim 通过事务 UPDATE ... WHERE state='received' 实现原子领取。
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      TEXT    NOT NULL,
    source_type     TEXT    NOT NULL,   -- comment / at / dm / danmaku / qq_share / proactive ...
    source_event_id TEXT    NOT NULL,   -- rpid / at_id / message_id / danmaku key
    actor_id        TEXT    NOT NULL,   -- 强制 namespace，如 bili:123456
    actor_name      TEXT    NOT NULL DEFAULT '',
    session_id      TEXT    NOT NULL,   -- 会话域，见 security.scopes
    target_id       TEXT    NOT NULL DEFAULT '',  -- bvid / oid / room_id
    thread_id       TEXT    NOT NULL DEFAULT '',
    priority        INTEGER NOT NULL DEFAULT 50,  -- 数值越小越优先
    ignore_level    TEXT    NOT NULL DEFAULT 'normal',
    state           TEXT    NOT NULL DEFAULT 'received',
    content         TEXT    NOT NULL DEFAULT '',
    content_hash    TEXT    NOT NULL DEFAULT '',
    payload         TEXT    NOT NULL DEFAULT '{}',
    attempts        INTEGER NOT NULL DEFAULT 0,
    draft           TEXT    NOT NULL DEFAULT '',
    send_fingerprint TEXT   NOT NULL DEFAULT '',
    verify_state    TEXT    NOT NULL DEFAULT '',  -- ''/pending/confirmed/absent
    error           TEXT    NOT NULL DEFAULT '',
    created_at      REAL    NOT NULL,
    updated_at      REAL    NOT NULL,
    claimed_at      REAL,
    sent_at         REAL,
    UNIQUE (account_id, source_type, source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_pick   ON events (state, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_events_actor  ON events (actor_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_target ON events (target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_hash   ON events (content_hash, created_at);

-- 事件状态流转审计，便于 WebUI 展示"这条为什么没发出去"。
CREATE TABLE IF NOT EXISTS event_transitions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    from_state TEXT   NOT NULL,
    to_state  TEXT    NOT NULL,
    reason    TEXT    NOT NULL DEFAULT '',
    at        REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_event ON event_transitions (event_id, at);

-- 写动作幂等表。key 由 security.capability 的 action digest 派生。
CREATE TABLE IF NOT EXISTS actions (
    key        TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    event_key  TEXT NOT NULL DEFAULT '',
    target_id  TEXT NOT NULL DEFAULT '',
    state      TEXT NOT NULL DEFAULT 'queued',  -- queued/running/succeeded/failed/unknown
    priority   INTEGER NOT NULL DEFAULT 40,
    attempts   INTEGER NOT NULL DEFAULT 0,
    digest     TEXT NOT NULL DEFAULT '',
    budget     TEXT NOT NULL DEFAULT '[]',
    detail     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_actions_state ON actions (state, created_at);

-- ---------------------------------------------------------------- 存储层
-- 记忆主表。正文与向量分离：embedding 单独一张表，避免读记忆时把
-- 1024 维 float 一起载入（旧实现把 embedding 存在 memory.json 里整文件重写）。
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,            -- 记忆域，见 security/scopes.py
    memory_type TEXT NOT NULL,            -- chat/video/dynamic/bangumi/live/user_summary/self
    level       TEXT NOT NULL DEFAULT 'recent',  -- recent/long_term/aged
    actor_id    TEXT NOT NULL DEFAULT '',
    thread_id   TEXT NOT NULL DEFAULT '',
    target_id   TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    importance  INTEGER NOT NULL DEFAULT 5,
    value_score REAL    NOT NULL DEFAULT 0.5,  -- 准入评分（可复用性）
    privacy     INTEGER NOT NULL DEFAULT 0,    -- 0 普通 1 敏感（不出域）
    confidence  REAL    NOT NULL DEFAULT 0.5,
    source_event INTEGER,                      -- 溯源到 events.id（可空）
    meta        TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    expires_at  REAL,                          -- TTL；NULL 表示随 level 老化
    promoted_at REAL,
    bytes       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_scope  ON memories (scope, level, created_at);
CREATE INDEX IF NOT EXISTS idx_mem_actor  ON memories (actor_id, created_at);
CREATE INDEX IF NOT EXISTS idx_mem_thread ON memories (thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_mem_expire ON memories (expires_at);

CREATE TABLE IF NOT EXISTS memory_vectors (
    memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    model     TEXT NOT NULL,
    dim       INTEGER NOT NULL,
    vec       BLOB NOT NULL           -- float32 紧凑存储，不进 JSON
);

-- 旧版 memory.json 与统一记忆表之间的稳定映射。旧字段保存在 memories.meta
-- 的 legacy 对象中；映射表只负责幂等迁移、兼容导出和按 rpid 精确删除。
-- 单独建表而不是给 memories 直接加列，可让已有 v1 数据库无损升级。
CREATE TABLE IF NOT EXISTS legacy_memory_map (
    memory_id  INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
    legacy_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_legacy_memory_key ON legacy_memory_map (legacy_key);

-- 永久视频去重账本。它只回答“是否看过”，不保存视频正文或向量；
-- 详细内容可以淡忘，但此表不按容量或时间裁剪。
CREATE TABLE IF NOT EXISTS seen_videos (
    bvid            TEXT PRIMARY KEY,
    first_seen_at   REAL NOT NULL,
    last_related_at REAL NOT NULL,
    watch_count     INTEGER NOT NULL DEFAULT 1,
    title           TEXT NOT NULL DEFAULT '',
    owner_mid       TEXT NOT NULL DEFAULT '',
    owner_name      TEXT NOT NULL DEFAULT '',
    tname           TEXT NOT NULL DEFAULT '',
    last_source     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_seen_videos_last ON seen_videos (last_related_at);

-- 对话反馈候选：只保存经过严格输出协议验证的短结论，并且必须在回复
-- 确认发送成功后写入。候选不会直接修改人格，由日报/周报后续聚合。
CREATE TABLE IF NOT EXISTS feedback_candidates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key        TEXT NOT NULL UNIQUE,
    actor_id         TEXT NOT NULL DEFAULT '',
    actor_name       TEXT NOT NULL DEFAULT '',
    scope            TEXT NOT NULL,
    feedback_type    TEXT NOT NULL,
    topic            TEXT NOT NULL DEFAULT '',
    event_summary    TEXT NOT NULL DEFAULT '',
    possible_mistake TEXT NOT NULL DEFAULT '',
    next_time        TEXT NOT NULL DEFAULT '',
    confidence       REAL NOT NULL DEFAULT 0,
    relation_weight  REAL NOT NULL DEFAULT 1,
    is_owner         INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'candidate',
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback_candidates (created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_topic ON feedback_candidates (feedback_type, topic);

-- 视频评价提取出的可验证偏好证据。evidence_key 由观看记录与具体信号共同
-- 生成，确保日报/周报重复汇总时不会把同一证据累计多次。
CREATE TABLE IF NOT EXISTS preference_evidence (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_key   TEXT NOT NULL UNIQUE,
    preference_key TEXT NOT NULL,
    signal_type    TEXT NOT NULL,
    value          TEXT NOT NULL,
    polarity       TEXT NOT NULL,
    strength       REAL NOT NULL DEFAULT 0,
    source_ref     TEXT NOT NULL DEFAULT '',
    occurred_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preference_evidence_key
    ON preference_evidence (preference_key, occurred_at);

-- 从证据推导出的当前偏好状态。候选/近期/稳定只是生命周期状态，不会改写
-- 核心人设；每周可增强、保留、减弱或删除。
CREATE TABLE IF NOT EXISTS preferences (
    preference_key TEXT PRIMARY KEY,
    signal_type    TEXT NOT NULL,
    value          TEXT NOT NULL,
    polarity       TEXT NOT NULL,
    stage          TEXT NOT NULL,
    score          REAL NOT NULL DEFAULT 0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    active_weeks   INTEGER NOT NULL DEFAULT 0,
    first_seen     REAL NOT NULL,
    last_seen      REAL NOT NULL,
    lifecycle_action TEXT NOT NULL DEFAULT 'retained',
    updated_at     REAL NOT NULL,
    expires_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preferences_stage
    ON preferences (stage, score, last_seen);

-- 用户群像：小体积结构化，增量更新（只改动变化字段，不重写全量摘要）。
CREATE TABLE IF NOT EXISTS profiles (
    actor_id     TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    familiarity  REAL NOT NULL DEFAULT 0.0,
    trust        REAL NOT NULL DEFAULT 0.0,
    warmth       REAL NOT NULL DEFAULT 0.0,
    conflict     REAL NOT NULL DEFAULT 0.0,
    stage        TEXT NOT NULL DEFAULT 'stranger',
    impression   TEXT NOT NULL DEFAULT '',
    topics       TEXT NOT NULL DEFAULT '[]',   -- 常聊主题（限长）
    avoid        TEXT NOT NULL DEFAULT '[]',   -- 应避免话题
    interact_count INTEGER NOT NULL DEFAULT 0,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    updated_at   REAL NOT NULL,
    revision     INTEGER NOT NULL DEFAULT 0
);

-- 群像事实。独立成行才能按条删除、按来源标注证据、按 TTL 过期。
CREATE TABLE IF NOT EXISTS profile_facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id   TEXT NOT NULL REFERENCES profiles(actor_id) ON DELETE CASCADE,
    fact       TEXT NOT NULL,
    scope      TEXT NOT NULL,
    evidence   TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    approved   INTEGER NOT NULL DEFAULT 0,   -- 高风险事实需管理员批准才注入
    created_at REAL NOT NULL,
    expires_at REAL,
    UNIQUE (actor_id, fact)
);
CREATE INDEX IF NOT EXISTS idx_facts_actor ON profile_facts (actor_id, created_at);

-- 视频/媒体理解缓存。摘要入库，原始媒体永不入库，只留引用与 TTL。
CREATE TABLE IF NOT EXISTS media_digests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,            -- video/image/dynamic/bangumi
    ref         TEXT NOT NULL,            -- bvid / 图片 URL hash / dynamic_id
    title       TEXT NOT NULL DEFAULT '',
    digest      TEXT NOT NULL DEFAULT '', -- 结构化事实摘要（阶段一产物）
    facts       TEXT NOT NULL DEFAULT '{}',
    tags        TEXT NOT NULL DEFAULT '[]',
    tokens_used INTEGER NOT NULL DEFAULT 0,
    cost_cents  REAL NOT NULL DEFAULT 0.0,
    created_at  REAL NOT NULL,
    expires_at  REAL,
    hits        INTEGER NOT NULL DEFAULT 0,
    last_hit_at REAL,
    UNIQUE (kind, ref)
);
CREATE INDEX IF NOT EXISTS idx_media_expire ON media_digests (expires_at);
CREATE INDEX IF NOT EXISTS idx_media_lru    ON media_digests (last_hit_at);

-- 周期总结（日报/周报）。只存结构化增量结果。
CREATE TABLE IF NOT EXISTS cycle_summaries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,             -- daily/weekly
    period_key TEXT NOT NULL,             -- 2026-08-14 / 2026-W33
    stats      TEXT NOT NULL DEFAULT '{}',
    text       TEXT NOT NULL DEFAULT '',
    delivered  TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL,
    expires_at REAL,
    UNIQUE (kind, period_key)
);

-- 通用 KV，替代散落的小 JSON（游标、开关、退避状态）。
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL
);

-- 计数器：限流桶与预算共用，按 (bucket, window_key) 累加。
CREATE TABLE IF NOT EXISTS counters (
    bucket     TEXT NOT NULL,
    window_key TEXT NOT NULL,
    count      REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bucket, window_key)
);
CREATE INDEX IF NOT EXISTS idx_counters_time ON counters (updated_at);

-- ---------------------------------------------------------------- 安全层
-- 一次性写能力票据。签发时绑定账号/目标/参数/调用者/会话，用后即焚。
CREATE TABLE IF NOT EXISTS capabilities (
    token      TEXT PRIMARY KEY,
    digest     TEXT NOT NULL,             -- 标准化 action digest
    tool       TEXT NOT NULL,
    account_id TEXT NOT NULL,
    caller_id  TEXT NOT NULL,
    session_id TEXT NOT NULL,
    args_hash  TEXT NOT NULL,
    issued_at  REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    state      TEXT NOT NULL DEFAULT 'issued'  -- issued/consumed/expired/revoked
);
CREATE INDEX IF NOT EXISTS idx_cap_state ON capabilities (state, expires_at);

-- 安全审计。工具调用、注入命中、脱敏拦截、权限拒绝都记这里。
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,             -- tool_call/denied/injection/redact/write
    tool       TEXT NOT NULL DEFAULT '',
    caller_id  TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    scope      TEXT NOT NULL DEFAULT '',
    decision   TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log (at);
CREATE INDEX IF NOT EXISTS idx_audit_kind ON audit_log (kind, at);

-- ---------------------------------------------------------------- 个性层
-- 生命状态快照，用于 WebUI 彩虹环与调度决策。单行滚动 + 历史留档。
CREATE TABLE IF NOT EXISTS life_states (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         REAL NOT NULL,
    mood       TEXT NOT NULL DEFAULT 'calm',
    energy     REAL NOT NULL DEFAULT 1.0,
    social     REAL NOT NULL DEFAULT 1.0,
    phase      TEXT NOT NULL DEFAULT 'idle',   -- 当前时段在干什么
    note       TEXT NOT NULL DEFAULT '',
    interests  TEXT NOT NULL DEFAULT '{}',
    budget     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_life_at ON life_states (at);

-- bot 自行规划的当日日程（含休息时段），彩虹环直接读它。
CREATE TABLE IF NOT EXISTS day_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date   TEXT NOT NULL,
    start_min   INTEGER NOT NULL,          -- 当天第几分钟（0-1439）
    end_min     INTEGER NOT NULL,
    activity    TEXT NOT NULL,             -- rest/browse/reply/watch/create/social/chores
    intent      TEXT NOT NULL DEFAULT '',
    energy_cost REAL NOT NULL DEFAULT 0.0,
    planned_by  TEXT NOT NULL DEFAULT 'bot',
    state       TEXT NOT NULL DEFAULT 'planned',  -- planned/active/done/skipped
    summary     TEXT NOT NULL DEFAULT '',
    stats       TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    UNIQUE (plan_date, start_min, activity)
);
CREATE INDEX IF NOT EXISTS idx_plan_date ON day_plans (plan_date, start_min);

-- open goals：答应过的事、想看完的内容。
CREATE TABLE IF NOT EXISTS goals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    text       TEXT NOT NULL,
    actor_id   TEXT NOT NULL DEFAULT '',
    priority   INTEGER NOT NULL DEFAULT 50,
    state      TEXT NOT NULL DEFAULT 'open',   -- open/done/dropped
    progress   REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL,
    due_at     REAL,
    closed_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_goals_state ON goals (state, priority);

-- 模型用量与预算。每次调用一行，按 purpose 归集，供 WebUI 与预算闸门使用。
CREATE TABLE IF NOT EXISTS model_usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           REAL NOT NULL,
    purpose      TEXT NOT NULL,        -- chat/persona/understand/ocr/asr/video/embed
    provider     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_cents   REAL NOT NULL DEFAULT 0.0,
    event_id     INTEGER,
    detail       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_usage_time    ON model_usage (at);
CREATE INDEX IF NOT EXISTS idx_usage_purpose ON model_usage (purpose, at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
