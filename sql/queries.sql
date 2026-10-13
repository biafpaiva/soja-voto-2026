-- =====================================================================
-- BDG 2026 | Soja x votação presidencial (1º turno) | SQLs do projeto
-- Servidor: PostgreSQL 17.5 + PostGIS 3.5.2 | Schema: geodata
--
-- PREMISSAS (AJUSTE os nomes se o seu banco for diferente):
--   geodata.munic  : cd_mun (IBGE, 7 díg.), nm_mun, sigla_uf, cd_tse,
--                    cd_rgi/nm_rgi (região imediata), cd_rgint/nm_rgint
--                    (região intermediária), geom (SIRGAS 2000, EPSG:4674)
--   geodata.beatriz_votos_presidente_2026_novo : CSV gerado pelo notebook
--                    (votos_presidente_2026_municipio_candidato.csv, ~69 mil linhas,
--                     12 candidatos, 5.757 "municípios" = 5.570 + exterior ZZ).
--                    Colunas originais em MAIÚSCULAS; ao importar, o DBeaver
--                    costuma converter para minúsculas:
--                    sg_uf, cd_municipio [código TSE], nm_municipio, nr_candidato,
--                    nm_candidato, nm_urna_candidato, sg_partido,
--                    qt_votos_nominais, qt_votos_nominais_validos
--                    (se o seu banco mantiver MAIÚSCULAS, use aspas: "QT_VOTOS_NOMINAIS")
--                    Candidatos: 13 = Lula (PT) | 22 = Flávio Bolsonaro (PL)
--   geodata.beatriz_agro_soja_pam_2025 : PAM/IBGE (tabela SIDRA 1612), soja em grão,
--                    uma linha por município: cd_mun, area_plantada_ha,
--                    area_colhida_ha, qtd_produzida_t, rendimento_kg_ha,
--                    valor_producao_mil_reais
--
-- As tabelas derivadas levam o prefixo beatriz_ (beatriz_soja26_*). Se não tiver permissão de
-- CREATE no schema compartilhado, troque "CREATE TABLE geodata.beatriz_soja26_x AS"
-- por "CREATE TEMP TABLE beatriz_soja26_x AS" (vale apenas na sessão).
-- =====================================================================


-- @@ 00 | Validação dos dados de entrada
-- Objetivo: confirmar candidatos, totais e cobertura ANTES de agregar.
-- Esperado: ~5.570 municípios (+ exterior, sg_uf = 'ZZ'); candidatos
-- presidenciais completos; total nacional compatível com o boletim do TSE.

-- 0.1 Ranking de candidatos (confira nomes de urna de Lula e Flávio)
SELECT nr_candidato, nm_urna_candidato, sg_partido,
       SUM(qt_votos_nominais) AS votos
FROM geodata.beatriz_votos_presidente_2026_novo
GROUP BY 1,2,3
ORDER BY votos DESC;

-- 0.2 Cobertura: municípios do TSE sem correspondência na malha do IBGE
SELECT v.sg_uf, v.cd_municipio, v.nm_municipio
FROM (SELECT DISTINCT sg_uf, cd_municipio, nm_municipio
      FROM geodata.beatriz_votos_presidente_2026_novo) v
LEFT JOIN geodata.munic m ON m.cd_tse::int = v.cd_municipio::int
WHERE m.cd_mun IS NULL;           -- esperado: apenas exterior (ZZ)

-- 0.3 Municípios da malha sem votos (esperado: 2 lagoas do RS 4300001/2)
SELECT m.cd_mun, m.nm_mun, m.sigla_uf
FROM geodata.munic m
LEFT JOIN (SELECT DISTINCT cd_municipio::int AS cd_tse
           FROM geodata.beatriz_votos_presidente_2026_novo) v
       ON v.cd_tse = m.cd_tse::int
WHERE v.cd_tse IS NULL;

-- 0.5 Algum município em que um terceiro candidato supera Lula e Flávio? (esperado: 0)
SELECT COUNT(*) AS municipios_com_outro_lider
FROM (SELECT cd_municipio,
             MAX(qt_votos_nominais) FILTER (WHERE nr_candidato::int NOT IN (13,22)) AS mx_outro,
             MAX(qt_votos_nominais) FILTER (WHERE nr_candidato::int IN (13,22))     AS mx_top2
      FROM geodata.beatriz_votos_presidente_2026_novo
      WHERE sg_uf <> 'ZZ' GROUP BY cd_municipio) t
WHERE mx_outro > mx_top2;

-- 0.4 Cobertura da PAM: municípios sem registro de soja (serão tratados como 0)
SELECT COUNT(*) AS municipios_malha,
       COUNT(s.cd_mun) AS com_registro_pam,
       COUNT(*) FILTER (WHERE s.area_plantada_ha > 0) AS produtores_soja
FROM geodata.munic m
LEFT JOIN geodata.beatriz_agro_soja_pam_2025 s ON s.cd_mun::text = m.cd_mun::text;


-- @@ 01 | Votos por município (Lula, Flávio Bolsonaro e total válido)
-- Denominador: soma dos votos nominais de TODOS os candidatos a presidente
-- no município (votos válidos). Brancos e nulos ficam fora do denominador.
-- Candidatos identificados pelo NÚMERO (13 = Lula, 22 = Flávio), mais robusto que nome
-- (o nome de urna vem sem acento: 'FLAVIO BOLSONARO').
DROP TABLE IF EXISTS geodata.beatriz_soja26_votos_mun;
CREATE TABLE geodata.beatriz_soja26_votos_mun AS
SELECT
    cd_municipio::int AS cd_tse,
    SUM(qt_votos_nominais)                                                        AS votos_validos,
    COALESCE(SUM(qt_votos_nominais) FILTER (WHERE nr_candidato::int = 13), 0) AS votos_lula,
    COALESCE(SUM(qt_votos_nominais) FILTER (WHERE nr_candidato::int = 22), 0) AS votos_flavio
FROM geodata.beatriz_votos_presidente_2026_novo
WHERE sg_uf <> 'ZZ'                      -- exclui voto no exterior
GROUP BY cd_municipio::int;

ALTER TABLE geodata.beatriz_soja26_votos_mun ADD PRIMARY KEY (cd_tse);


-- @@ 02 | Base municipal integrada (geometria-chave + votos + soja)
-- Uma linha por município da malha IBGE 2025. Decisões:
--  * area_km2 calculada sobre geography (m²/1e6), em vez de projeção plana;
--  * município sem registro na PAM => soja = 0 (PAM não lista quem não produz);
--  * pct_area_soja = área plantada (ha) / área do município (ha) x 100.
--    Como 1 km² = 100 ha, o resultado é simplesmente ha / km²;
--  * pct_* ficam NULL onde não há votos (lagoas 4300001 e 4300002).
-- COVARIÁVEIS DO CENSO (opcional): faça LEFT JOIN com suas tabelas do Censo e
-- inclua colunas com estes nomes, que o app reconhece automaticamente:
--   populacao, renda_media, pct_superior, pct_evangelicos, pct_urbana
DROP TABLE IF EXISTS geodata.beatriz_soja26_base_mun;
CREATE TABLE geodata.beatriz_soja26_base_mun AS
SELECT
    m.cd_mun::text                         AS cd_mun,
    m.nm_mun,
    m.sigla_uf,
    m.cd_rgi::text                         AS cd_micro,
    m.nm_rgi                               AS nm_micro,
    m.cd_rgint::text                       AS cd_meso,
    m.nm_rgint                             AS nm_meso,
    ROUND((ST_Area(m.geom::geography)/1e6)::numeric, 2) AS area_km2,
    v.votos_validos,
    v.votos_lula,
    v.votos_flavio,
    ROUND(100.0 * v.votos_lula   / NULLIF(v.votos_validos,0), 3) AS pct_lula,
    ROUND(100.0 * v.votos_flavio / NULLIF(v.votos_validos,0), 3) AS pct_flavio,
    ROUND(100.0 * (v.votos_validos - v.votos_lula - v.votos_flavio)
                / NULLIF(v.votos_validos,0), 3)                   AS pct_outros,
    ROUND(100.0 * (v.votos_lula - v.votos_flavio)
                / NULLIF(v.votos_validos,0), 3)                   AS margem_pp,   -- Lula - Flávio
    COALESCE(s.area_plantada_ha, 0)        AS soja_area_plantada_ha,
    COALESCE(s.area_colhida_ha, 0)         AS soja_area_colhida_ha,
    COALESCE(s.qtd_produzida_t, 0)         AS soja_prod_t,
    s.rendimento_kg_ha                     AS soja_rend_kg_ha,
    COALESCE(s.valor_producao_mil_reais,0) AS soja_valor_mil,
    COALESCE(s.area_plantada_ha, 0) > 0    AS produz_soja
FROM geodata.munic m
LEFT JOIN geodata.beatriz_soja26_votos_mun v ON v.cd_tse = m.cd_tse::int
LEFT JOIN geodata.beatriz_agro_soja_pam_2025   s ON s.cd_mun::text = m.cd_mun::text;

ALTER TABLE geodata.beatriz_soja26_base_mun
  ADD COLUMN pct_area_soja numeric;
UPDATE geodata.beatriz_soja26_base_mun
   SET pct_area_soja = ROUND((soja_area_plantada_ha / NULLIF(area_km2,0))::numeric, 3);

-- Vencedor e categoria do mapa "Voto nos municípios" (estilo jornal):
--   LULA_MAIS / FLAVIO_MAIS   = vitória com 50,01% ou mais dos votos válidos
--   LULA_MENOS / FLAVIO_MENOS = vitória com menos de 50,01%
ALTER TABLE geodata.beatriz_soja26_base_mun
  ADD COLUMN vencedor text, ADD COLUMN pct_vencedor numeric, ADD COLUMN categoria_vitoria text;
UPDATE geodata.beatriz_soja26_base_mun
   SET vencedor = CASE WHEN votos_validos IS NULL THEN NULL
                       WHEN votos_lula >= votos_flavio THEN 'LULA' ELSE 'FLAVIO' END,
       pct_vencedor = GREATEST(pct_lula, pct_flavio),
       categoria_vitoria = CASE
            WHEN votos_validos IS NULL THEN NULL
            WHEN votos_lula >= votos_flavio AND pct_lula   >= 50.01 THEN 'LULA_MAIS'
            WHEN votos_lula >= votos_flavio                         THEN 'LULA_MENOS'
            WHEN pct_flavio >= 50.01                                THEN 'FLAVIO_MAIS'
            ELSE 'FLAVIO_MENOS' END;

ALTER TABLE geodata.beatriz_soja26_base_mun ADD PRIMARY KEY (cd_mun);


-- @@ 02b | Religião (Censo 2022, SIDRA 10198): evangélicos x católicos
-- PASSO A: DESCOBERTA. Rode estas duas consultas e veja como a SUA tabela está montada.
-- Premissa: geodata.tabela10198_religiao com colunas cd_mun, religiao, sexo, idade, valor
-- (ajuste o nome da tabela e das colunas abaixo se forem diferentes).
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'geodata' AND table_name = 'religiao';

SELECT religiao, COUNT(*) AS linhas FROM geodata.religiao GROUP BY 1 ORDER BY 1;
-- Veja também os valores distintos de sexo e idade (queremos o recorte "Total"):
-- SELECT DISTINCT sexo, idade FROM geodata.tabela10198_religiao;

-- PASSO B: tabela por município. A CTE "mapa" liga o RÓTULO EXATO de cada religião
-- (como aparece no PASSO A) ao grupo. EDITE os rótulos se forem diferentes.
-- Não inclua subcategorias junto da categoria-mãe (ex.: "Evangélica" e
-- "Evangélica de missão"), senão há contagem em dobro.
-- Valores não numéricos do SIDRA ("-", "X", "...") viram 0.
DROP TABLE IF EXISTS geodata.beatriz_relig_mun;

CREATE TABLE geodata.beatriz_relig_mun as
SELECT
    codigo::text AS cd_mun,

    (
        COALESCE(cat_romana, 0) +
        COALESCE(evangelicas, 0) +
        COALESCE(espirita, 0) +
        COALESCE(umbanda_cand, 0) +
        COALESCE(trad_indig, 0) +
        COALESCE(outras_relig, 0) +
        COALESCE(sem_relig, 0) +
        COALESCE(nao_sabe, 0)
    ) AS pop_total,

    COALESCE(cat_romana, 0) AS pop_catolicos,

    COALESCE(evangelicas, 0) AS pop_evangelicos,

    COALESCE(sem_relig, 0) AS pop_sem_religiao

FROM geodata.religiao;

-- Checagem: devem aparecer ~5.570 municípios e nenhum com soma de grupos > total
SELECT COUNT(*) AS municipios,
       COUNT(*) FILTER (WHERE pop_catolicos + pop_evangelicos + pop_sem_religiao > pop_total) AS inconsistentes
FROM geodata.beatriz_relig_mun;

-- PASSO C: acrescenta as colunas à base municipal (o app as reconhece pelos nomes).
ALTER TABLE geodata.beatriz_soja26_base_mun
  ADD COLUMN IF NOT EXISTS populacao numeric,
  ADD COLUMN IF NOT EXISTS pop_catolicos numeric,
  ADD COLUMN IF NOT EXISTS pop_evangelicos numeric,
  ADD COLUMN IF NOT EXISTS pop_sem_religiao numeric,
  ADD COLUMN IF NOT EXISTS pct_catolicos numeric,
  ADD COLUMN IF NOT EXISTS pct_evangelicos numeric,
  ADD COLUMN IF NOT EXISTS pct_sem_religiao numeric;

UPDATE geodata.beatriz_soja26_base_mun b
   SET populacao        = r.pop_total,
       pop_catolicos    = r.pop_catolicos,
       pop_evangelicos  = r.pop_evangelicos,
       pop_sem_religiao = r.pop_sem_religiao,
       pct_catolicos    = ROUND(100.0 * r.pop_catolicos    / NULLIF(r.pop_total,0), 3),
       pct_evangelicos  = ROUND(100.0 * r.pop_evangelicos  / NULLIF(r.pop_total,0), 3),
       pct_sem_religiao = ROUND(100.0 * r.pop_sem_religiao / NULLIF(r.pop_total,0), 3)
  FROM geodata.beatriz_relig_mun r
 WHERE r.cd_mun = b.cd_mun;
-- Municípios sem Censo 2022 (Boa Esperança do Norte, lagoas) ficam NULL, como esperado.
-- IMPORTANTE: depois deste bloco, (re)execute os blocos 03 e 04 para que as regiões
-- imediatas/intermediárias também recebam os totais religiosos.


-- @@ 03 | Agregação por Região Geográfica Imediata (micro)
-- Somam-se contagens (votos, área, produção, população por religião) e só depois recalculam-se os
-- percentuais — nunca se tira a média dos percentuais municipais.
DROP TABLE IF EXISTS geodata.beatriz_soja26_base_micro;
CREATE TABLE geodata.beatriz_soja26_base_micro AS
SELECT
    cd_micro,
    MAX(nm_micro)  AS nm_micro,
    MAX(sigla_uf)  AS sigla_uf,
    COUNT(*)       AS n_mun,
    SUM(area_km2)  AS area_km2,
    SUM(votos_validos) AS votos_validos,
    SUM(votos_lula)    AS votos_lula,
    SUM(votos_flavio)  AS votos_flavio,
    ROUND(100.0*SUM(votos_lula)  /NULLIF(SUM(votos_validos),0),3) AS pct_lula,
    ROUND(100.0*SUM(votos_flavio)/NULLIF(SUM(votos_validos),0),3) AS pct_flavio,
    ROUND(100.0*(SUM(votos_lula)-SUM(votos_flavio))/NULLIF(SUM(votos_validos),0),3) AS margem_pp,
    SUM(soja_area_plantada_ha) AS soja_area_plantada_ha,
    SUM(soja_prod_t)           AS soja_prod_t,
    SUM(soja_valor_mil)        AS soja_valor_mil,
    SUM(populacao)        AS populacao,
    SUM(pop_catolicos)    AS pop_catolicos,
    SUM(pop_evangelicos)  AS pop_evangelicos,
    ROUND(100.0*SUM(pop_catolicos)  /NULLIF(SUM(populacao),0),3) AS pct_catolicos,
    ROUND(100.0*SUM(pop_evangelicos)/NULLIF(SUM(populacao),0),3) AS pct_evangelicos,
    ROUND((SUM(soja_area_plantada_ha)/NULLIF(SUM(area_km2),0))::numeric,3) AS pct_area_soja,
    COUNT(*) FILTER (WHERE produz_soja) AS n_mun_produtores
FROM geodata.beatriz_soja26_base_mun
WHERE votos_validos IS NOT NULL
GROUP BY cd_micro;


-- @@ 04 | Agregação por Região Geográfica Intermediária (meso)
DROP TABLE IF EXISTS geodata.beatriz_soja26_base_meso;
CREATE TABLE geodata.beatriz_soja26_base_meso AS
SELECT
    cd_meso,
    MAX(nm_meso)  AS nm_meso,
    MAX(sigla_uf) AS sigla_uf,
    COUNT(*)      AS n_mun,
    SUM(area_km2) AS area_km2,
    SUM(votos_validos) AS votos_validos,
    SUM(votos_lula)    AS votos_lula,
    SUM(votos_flavio)  AS votos_flavio,
    ROUND(100.0*SUM(votos_lula)  /NULLIF(SUM(votos_validos),0),3) AS pct_lula,
    ROUND(100.0*SUM(votos_flavio)/NULLIF(SUM(votos_validos),0),3) AS pct_flavio,
    ROUND(100.0*(SUM(votos_lula)-SUM(votos_flavio))/NULLIF(SUM(votos_validos),0),3) AS margem_pp,
    SUM(soja_area_plantada_ha) AS soja_area_plantada_ha,
    SUM(soja_prod_t)           AS soja_prod_t,
    SUM(soja_valor_mil)        AS soja_valor_mil,
    SUM(populacao)        AS populacao,
    SUM(pop_catolicos)    AS pop_catolicos,
    SUM(pop_evangelicos)  AS pop_evangelicos,
    ROUND(100.0*SUM(pop_catolicos)  /NULLIF(SUM(populacao),0),3) AS pct_catolicos,
    ROUND(100.0*SUM(pop_evangelicos)/NULLIF(SUM(populacao),0),3) AS pct_evangelicos,
    ROUND((SUM(soja_area_plantada_ha)/NULLIF(SUM(area_km2),0))::numeric,3) AS pct_area_soja,
    COUNT(*) FILTER (WHERE produz_soja) AS n_mun_produtores
FROM geodata.beatriz_soja26_base_mun
WHERE votos_validos IS NOT NULL
GROUP BY cd_meso;


-- @@ 05 | Exportação dos CSVs (rodar no DBeaver > Export data > CSV, UTF-8)
-- Salvar em data/ com os nomes abaixo:
--   base_mun.csv | base_micro.csv | base_meso.csv  (+ estados.geojson, SQL 07)
SELECT * FROM geodata.beatriz_soja26_base_mun   ORDER BY cd_mun;     -- -> data/base_mun.csv
SELECT * FROM geodata.beatriz_soja26_base_micro ORDER BY cd_micro;   -- -> data/base_micro.csv
SELECT * FROM geodata.beatriz_soja26_base_meso  ORDER BY cd_meso;    -- -> data/base_meso.csv


-- @@ 06 | Geometria municipal simplificada (GeoJSON) para mapas e vizinhança
-- ST_SimplifyPreserveTopology (~0,005° ≈ 500 m) mantém a topologia (os
-- polígonos vizinhos continuam compartilhando fronteiras), reduzindo o
-- arquivo para dezenas de MB. Copie o valor único retornado e salve em
-- data/municipios.geojson (DBeaver: clique direito na célula > Save to file).
SELECT json_build_object(
         'type', 'FeatureCollection',
         'features', json_agg(ST_AsGeoJSON(t.*, 'geom', 5)::json)
       ) AS geojson
FROM (
    SELECT cd_mun::text AS cd_mun, nm_mun,
           ST_SimplifyPreserveTopology(ST_Transform(geom, 4326), 0.005) AS geom
    FROM geodata.munic
) t;
-- Alternativa por linha de comando (GDAL):
--   ogr2ogr -f GeoJSON data/municipios.geojson \
--     "PG:host=150.164.2.42 dbname=bdg2026 user=turma2026 password=turma" \
--     -sql "SELECT cd_mun::text cd_mun, nm_mun, ST_SimplifyPreserveTopology(ST_Transform(geom,4326),0.005) geom FROM geodata.munic" \
--     -nln municipios


-- @@ 07 | Limites estaduais (GeoJSON) para sobrepor aos mapas (linhas brancas)
-- Salvar como data/estados.geojson. Tolerância maior (0,02°) pois são só contornos.
SELECT json_build_object(
         'type', 'FeatureCollection',
         'features', json_agg(ST_AsGeoJSON(t.*, 'geom', 4)::json)
       ) AS geojson
FROM (
    SELECT sigla_uf,
           ST_SimplifyPreserveTopology(ST_Transform(geom, 4326), 0.02) AS geom
    FROM geodata.estado
) t;
