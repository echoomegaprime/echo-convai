-- Echo ConvAI D1 Schema
-- ElevenLabs-compatible agent + conversation storage

-- API Keys for authentication
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  key_hash TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT 'default',
  permissions TEXT NOT NULL DEFAULT '["all"]',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_used_at TEXT,
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_workspace ON api_keys(workspace_id);

-- Agents
CREATE TABLE IF NOT EXISTS agents (
  agent_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',
  conversation_config TEXT NOT NULL DEFAULT '{}',
  platform_settings TEXT NOT NULL DEFAULT '{}',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  is_archived INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_agents_workspace ON agents(workspace_id);
CREATE INDEX IF NOT EXISTS idx_agents_name ON agents(name);
CREATE INDEX IF NOT EXISTS idx_agents_created ON agents(created_at);

-- Conversations
CREATE TABLE IF NOT EXISTS conversations (
  conversation_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  start_time TEXT NOT NULL DEFAULT (datetime('now')),
  end_time TEXT,
  duration_secs REAL DEFAULT 0,
  metadata TEXT NOT NULL DEFAULT '{}',
  analysis TEXT,
  call_successful INTEGER,
  rating REAL,
  feedback_comment TEXT,
  user_id TEXT,
  FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_conv_agent ON conversations(agent_id);
CREATE INDEX IF NOT EXISTS idx_conv_workspace ON conversations(workspace_id);
CREATE INDEX IF NOT EXISTS idx_conv_start ON conversations(start_time);
CREATE INDEX IF NOT EXISTS idx_conv_status ON conversations(status);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);

-- Conversation Messages
CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('user', 'agent', 'system', 'tool_call', 'tool_result')),
  content TEXT NOT NULL DEFAULT '',
  timestamp TEXT NOT NULL DEFAULT (datetime('now')),
  metadata TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_msg_timestamp ON messages(timestamp);

-- Knowledge Base Documents
CREATE TABLE IF NOT EXISTS knowledge_documents (
  document_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  mime_type TEXT NOT NULL DEFAULT 'application/octet-stream',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  r2_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'uploaded' CHECK(status IN ('uploading', 'uploaded', 'processing', 'ready', 'error')),
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_kb_agent ON knowledge_documents(agent_id);
CREATE INDEX IF NOT EXISTS idx_kb_workspace ON knowledge_documents(workspace_id);

-- Signed URL tokens (for WebSocket auth)
CREATE TABLE IF NOT EXISTS signed_url_tokens (
  token_id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_token_agent ON signed_url_tokens(agent_id);
CREATE INDEX IF NOT EXISTS idx_token_expires ON signed_url_tokens(expires_at);
