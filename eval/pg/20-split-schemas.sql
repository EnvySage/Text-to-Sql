-- 由 eval/datasets/bird_minidev/minidev/MINIDEV/dev_tables.json 生成。
--
-- 官方导出把 11 个库的 75 张表全放在 public 里。按 db_id 拆进各自的 schema，
-- 评测时用 search_path 切换：每道题只看得到自己那个库的表，和 SQLite 一库一文件的设定一致，
-- 否则每道题的 schema 都是 75 张表，prompt 长度和难度都变了，两种方言的数字没法比。

CREATE SCHEMA california_schools AUTHORIZATION xiaolongli;
ALTER TABLE public."frpm" SET SCHEMA california_schools;
ALTER TABLE public."satscores" SET SCHEMA california_schools;
ALTER TABLE public."schools" SET SCHEMA california_schools;

CREATE SCHEMA card_games AUTHORIZATION xiaolongli;
ALTER TABLE public."cards" SET SCHEMA card_games;
ALTER TABLE public."foreign_data" SET SCHEMA card_games;
ALTER TABLE public."legalities" SET SCHEMA card_games;
ALTER TABLE public."sets" SET SCHEMA card_games;
ALTER TABLE public."set_translations" SET SCHEMA card_games;
ALTER TABLE public."rulings" SET SCHEMA card_games;

CREATE SCHEMA codebase_community AUTHORIZATION xiaolongli;
ALTER TABLE public."badges" SET SCHEMA codebase_community;
ALTER TABLE public."comments" SET SCHEMA codebase_community;
ALTER TABLE public."posthistory" SET SCHEMA codebase_community;
ALTER TABLE public."postlinks" SET SCHEMA codebase_community;
ALTER TABLE public."posts" SET SCHEMA codebase_community;
ALTER TABLE public."tags" SET SCHEMA codebase_community;
ALTER TABLE public."users" SET SCHEMA codebase_community;
ALTER TABLE public."votes" SET SCHEMA codebase_community;

CREATE SCHEMA debit_card_specializing AUTHORIZATION xiaolongli;
ALTER TABLE public."customers" SET SCHEMA debit_card_specializing;
ALTER TABLE public."gasstations" SET SCHEMA debit_card_specializing;
ALTER TABLE public."products" SET SCHEMA debit_card_specializing;
ALTER TABLE public."transactions_1k" SET SCHEMA debit_card_specializing;
ALTER TABLE public."yearmonth" SET SCHEMA debit_card_specializing;

CREATE SCHEMA european_football_2 AUTHORIZATION xiaolongli;
ALTER TABLE public."player_attributes" SET SCHEMA european_football_2;
ALTER TABLE public."player" SET SCHEMA european_football_2;
ALTER TABLE public."league" SET SCHEMA european_football_2;
ALTER TABLE public."country" SET SCHEMA european_football_2;
ALTER TABLE public."team" SET SCHEMA european_football_2;
ALTER TABLE public."team_attributes" SET SCHEMA european_football_2;
ALTER TABLE public."match" SET SCHEMA european_football_2;

CREATE SCHEMA financial AUTHORIZATION xiaolongli;
ALTER TABLE public."account" SET SCHEMA financial;
ALTER TABLE public."card" SET SCHEMA financial;
ALTER TABLE public."client" SET SCHEMA financial;
ALTER TABLE public."disp" SET SCHEMA financial;
ALTER TABLE public."district" SET SCHEMA financial;
ALTER TABLE public."loan" SET SCHEMA financial;
ALTER TABLE public."order" SET SCHEMA financial;
ALTER TABLE public."trans" SET SCHEMA financial;

CREATE SCHEMA formula_1 AUTHORIZATION xiaolongli;
ALTER TABLE public."circuits" SET SCHEMA formula_1;
ALTER TABLE public."constructors" SET SCHEMA formula_1;
ALTER TABLE public."drivers" SET SCHEMA formula_1;
ALTER TABLE public."seasons" SET SCHEMA formula_1;
ALTER TABLE public."races" SET SCHEMA formula_1;
ALTER TABLE public."constructorresults" SET SCHEMA formula_1;
ALTER TABLE public."constructorstandings" SET SCHEMA formula_1;
ALTER TABLE public."driverstandings" SET SCHEMA formula_1;
ALTER TABLE public."laptimes" SET SCHEMA formula_1;
ALTER TABLE public."pitstops" SET SCHEMA formula_1;
ALTER TABLE public."qualifying" SET SCHEMA formula_1;
ALTER TABLE public."status" SET SCHEMA formula_1;
ALTER TABLE public."results" SET SCHEMA formula_1;

CREATE SCHEMA student_club AUTHORIZATION xiaolongli;
ALTER TABLE public."event" SET SCHEMA student_club;
ALTER TABLE public."major" SET SCHEMA student_club;
ALTER TABLE public."zip_code" SET SCHEMA student_club;
ALTER TABLE public."attendance" SET SCHEMA student_club;
ALTER TABLE public."budget" SET SCHEMA student_club;
ALTER TABLE public."expense" SET SCHEMA student_club;
ALTER TABLE public."income" SET SCHEMA student_club;
ALTER TABLE public."member" SET SCHEMA student_club;

CREATE SCHEMA superhero AUTHORIZATION xiaolongli;
ALTER TABLE public."alignment" SET SCHEMA superhero;
ALTER TABLE public."attribute" SET SCHEMA superhero;
ALTER TABLE public."colour" SET SCHEMA superhero;
ALTER TABLE public."gender" SET SCHEMA superhero;
ALTER TABLE public."publisher" SET SCHEMA superhero;
ALTER TABLE public."race" SET SCHEMA superhero;
ALTER TABLE public."superhero" SET SCHEMA superhero;
ALTER TABLE public."hero_attribute" SET SCHEMA superhero;
ALTER TABLE public."superpower" SET SCHEMA superhero;
ALTER TABLE public."hero_power" SET SCHEMA superhero;

CREATE SCHEMA thrombosis_prediction AUTHORIZATION xiaolongli;
ALTER TABLE public."examination" SET SCHEMA thrombosis_prediction;
ALTER TABLE public."patient" SET SCHEMA thrombosis_prediction;
ALTER TABLE public."laboratory" SET SCHEMA thrombosis_prediction;

CREATE SCHEMA toxicology AUTHORIZATION xiaolongli;
ALTER TABLE public."atom" SET SCHEMA toxicology;
ALTER TABLE public."bond" SET SCHEMA toxicology;
ALTER TABLE public."connected" SET SCHEMA toxicology;
ALTER TABLE public."molecule" SET SCHEMA toxicology;

-- 拆完 public 里不应再剩任何表；剩了说明映射漏了，宁可初始化失败也不要带病评测。
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'public') THEN
    RAISE EXCEPTION 'public 里还有未归属的表';
  END IF;
END $$;

-- 只读账号：沙箱连接层防线的前提（见 sandbox/postgres.py 的账号自检）。
CREATE ROLE agent_ro LOGIN PASSWORD 'agent_ro';
GRANT CONNECT ON DATABASE bird TO agent_ro;
GRANT USAGE ON SCHEMA california_schools TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA california_schools TO agent_ro;
GRANT USAGE ON SCHEMA card_games TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA card_games TO agent_ro;
GRANT USAGE ON SCHEMA codebase_community TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA codebase_community TO agent_ro;
GRANT USAGE ON SCHEMA debit_card_specializing TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA debit_card_specializing TO agent_ro;
GRANT USAGE ON SCHEMA european_football_2 TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA european_football_2 TO agent_ro;
GRANT USAGE ON SCHEMA financial TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA financial TO agent_ro;
GRANT USAGE ON SCHEMA formula_1 TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA formula_1 TO agent_ro;
GRANT USAGE ON SCHEMA student_club TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA student_club TO agent_ro;
GRANT USAGE ON SCHEMA superhero TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA superhero TO agent_ro;
GRANT USAGE ON SCHEMA thrombosis_prediction TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA thrombosis_prediction TO agent_ro;
GRANT USAGE ON SCHEMA toxicology TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA toxicology TO agent_ro;
