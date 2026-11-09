CREATE TABLE IF NOT EXISTS "account" ( "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "name" varchar NOT NULL );

CREATE UNIQUE INDEX IF NOT EXISTS "uq_account_name" ON "account" ("name");

CREATE TABLE IF NOT EXISTS "item" ( "account_id" integer, "id" integer NOT NULL PRIMARY KEY AUTOINCREMENT, "title" varchar NOT NULL, CONSTRAINT "fk_item_account_id_account" FOREIGN KEY ("account_id") REFERENCES "account" ("id") ON DELETE SET NULL );

CREATE INDEX IF NOT EXISTS "idx_item_title" ON "item" ("title");
