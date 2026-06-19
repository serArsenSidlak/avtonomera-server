-- AutoCheck — окрема БД перевірки авто (НЕ Supabase: завеликий обсяг).
-- Джерело: МВС «Відомості про транспортні засоби та їх власників» (data.gov.ua,
-- набір 0ffd8b75-0628-48cc-952a-9302f9799ec0). Деперсоналізовано (без ПІБ власників).
-- Кожен рядок = одна реєстраційна операція; історія авто = кілька рядків з одним VIN/номером.

CREATE TABLE IF NOT EXISTS vehicle_ops (
    id            bigserial PRIMARY KEY,
    vin           text,            -- повний VIN (з 2021 у відкритих даних)
    plate         text,            -- N_REG_NEW, нормалізований у кирилицю, без пробілів
    brand         text,            -- BRAND
    model         text,            -- MODEL
    make_year     int,             -- MAKE_YEAR
    color         text,            -- COLOR
    kind          text,            -- KIND (ЛЕГКОВИЙ/ВАНТАЖНИЙ/…)
    body          text,            -- BODY (СЕДАН/ХЕТЧБЕК/…)
    purpose       text,            -- PURPOSE
    fuel          text,            -- FUEL
    capacity      int,             -- обʼєм двигуна, см³
    own_weight    int,             -- OWN_WEIGHT
    total_weight  int,             -- TOTAL_WEIGHT
    d_reg         date,            -- D_REG (дата операції)
    oper_code     int,             -- OPER_CODE
    oper_name     text,            -- OPER_NAME (тип операції)
    dep_code      int,             -- DEP_CODE
    dep           text,            -- DEP (ТСЦ)
    reg_addr_koatuu text,          -- REG_ADDR_KOATUU (КОАТУУ адреси реєстрації)
    person        text,            -- PERSON (P=фізособа, J=юрособа)
    src_year      int              -- з якого річного файлу завантажено
);

-- Пошук по номеру та VIN — головні запити AutoCheck.
CREATE INDEX IF NOT EXISTS ix_vops_plate ON vehicle_ops (plate);
CREATE INDEX IF NOT EXISTS ix_vops_vin   ON vehicle_ops (vin);

-- Журнал завантажень (щоб не заливати той самий період двічі / бачити актуальність).
CREATE TABLE IF NOT EXISTS import_log (
    id          serial PRIMARY KEY,
    src_year    int,
    resource_id text,
    file_name   text,
    rows        int,
    imported_at timestamptz DEFAULT now()
);
