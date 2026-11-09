CREATE TABLE IF NOT EXISTS "player" ( "id" serial PRIMARY KEY NOT NULL );

CREATE TABLE IF NOT EXISTS "tournament" ( "id" serial PRIMARY KEY NOT NULL );

CREATE TABLE IF NOT EXISTS "player_tournaments" ( "player_id" integer NOT NULL, "tournament_id" integer NOT NULL, CONSTRAINT "fk_player_tournaments_player_id_player" FOREIGN KEY ("player_id") REFERENCES "player" ("id") ON DELETE CASCADE, CONSTRAINT "fk_player_tournaments_tournament_id_tournament" FOREIGN KEY ("tournament_id") REFERENCES "tournament" ("id") ON DELETE CASCADE );

CREATE INDEX IF NOT EXISTS "idx_player_tournaments_tournament_id_player_id" ON "player_tournaments" ("tournament_id", "player_id");

CREATE UNIQUE INDEX IF NOT EXISTS "uq_player_tournaments_player_id_tournament_id" ON "player_tournaments" ("player_id", "tournament_id");
