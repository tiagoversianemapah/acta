-- Desenho das tabelas do banco.
-- Pode rodar várias vezes sem problema: só cria o que ainda não existe.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS empresa (
    id             INTEGER PRIMARY KEY,
    documento      TEXT NOT NULL UNIQUE,
    tipo_documento TEXT NOT NULL CHECK (tipo_documento IN ('CNPJ','CPF')),
    nome           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lote (
    id             INTEGER PRIMARY KEY,
    descricao      TEXT NOT NULL,
    arquivo_origem TEXT,
    criado_em      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    encerrado_em   TEXT
);

CREATE TABLE IF NOT EXISTS job (
    id                  INTEGER PRIMARY KEY,
    lote_id             INTEGER NOT NULL REFERENCES lote(id),
    empresa_id          INTEGER NOT NULL REFERENCES empresa(id),
    orgao               TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING','RUNNING','RETRY_WAIT','DONE','FAILED')),
    desfecho            TEXT,
    tentativas          INTEGER NOT NULL DEFAULT 0,
    proxima_execucao_em TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    atualizado_em       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (lote_id, empresa_id, orgao)
);

CREATE INDEX IF NOT EXISTS idx_job_fila
    ON job (orgao, status, proxima_execucao_em);

CREATE TABLE IF NOT EXISTS tentativa (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES job(id),
    numero          INTEGER NOT NULL,
    iniciada_em     TEXT NOT NULL,
    finalizada_em   TEXT,
    desfecho        TEXT,
    mensagem_portal TEXT,
    evidencia       TEXT,
    worker          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tentativa_job ON tentativa (job_id);

CREATE TABLE IF NOT EXISTS certidao (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES job(id),
    tipo            TEXT NOT NULL CHECK (tipo IN ('NEGATIVA','CPEN')),
    emitida_em      TEXT NOT NULL,
    valida_ate      TEXT,
    codigo_controle TEXT,
    caminho_pdf     TEXT NOT NULL,
    sha256          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_certidao_job ON certidao (job_id);

-- Estado do robô (sobrevive a desligar e ligar de novo)

CREATE TABLE IF NOT EXISTS ritmo (
    orgao            TEXT PRIMARY KEY,
    intervalo_s      REAL NOT NULL,
    consultas_limpas INTEGER NOT NULL DEFAULT 0,
    atualizado_em    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS breaker (
    orgao         TEXT PRIMARY KEY,
    estado        TEXT NOT NULL DEFAULT 'FECHADO'
                  CHECK (estado IN ('FECHADO','ABERTO','MEIO_ABERTO')),
    aberto_ate    TEXT,
    aberturas     INTEGER NOT NULL DEFAULT 0,
    motivo        TEXT,
    atualizado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeat (
    processo      TEXT PRIMARY KEY,
    atualizado_em TEXT NOT NULL
);
