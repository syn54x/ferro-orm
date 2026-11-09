CREATE TABLE IF NOT EXISTS "account" ( "id" serial PRIMARY KEY NOT NULL, "name" varchar NOT NULL );

CREATE UNIQUE INDEX IF NOT EXISTS "uq_account_name" ON "account" ("name");

CREATE TABLE IF NOT EXISTS "item" ( "account_id" integer, "id" serial PRIMARY KEY NOT NULL, "title" varchar NOT NULL, CONSTRAINT "fk_item_account_id_account" FOREIGN KEY ("account_id") REFERENCES "account" ("id") ON DELETE SET NULL );

CREATE INDEX IF NOT EXISTS "idx_item_title" ON "item" ("title");
