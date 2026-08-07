CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS fg_import_batch (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  phase text NOT NULL,
  source_filename text,
  status text NOT NULL DEFAULT 'imported',
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fg_graph_node (
  node_key text PRIMARY KEY,
  labels text[] NOT NULL,
  properties jsonb NOT NULL DEFAULT '{}'::jsonb,
  phase text,
  source_batch_id uuid REFERENCES fg_import_batch(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fg_graph_relationship (
  relationship_key text PRIMARY KEY,
  from_node_key text NOT NULL,
  to_node_key text NOT NULL,
  relationship_type text NOT NULL,
  properties jsonb NOT NULL DEFAULT '{}'::jsonb,
  phase text,
  source_batch_id uuid REFERENCES fg_import_batch(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fg_graph_node_phase ON fg_graph_node(phase);
CREATE INDEX IF NOT EXISTS idx_fg_graph_rel_phase ON fg_graph_relationship(phase);
CREATE INDEX IF NOT EXISTS idx_fg_graph_rel_type ON fg_graph_relationship(relationship_type);
