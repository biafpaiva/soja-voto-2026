"""
Relatório interativo: Soja x votação presidencial 2026 (1º turno)
BDG 2026 - UFMG / DCC.   Execute com:  streamlit run app.py
Estrutura esperada:
  app.py | sql/queries.sql | data/{base_mun,base_micro,base_meso}.csv, data/municipios.geojson
"""
import io
import json
import re
from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd
import plotly.express as px
import statsmodels.formula.api as smf
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, to_hex
from matplotlib.patches import Patch
import streamlit as st
from esda.moran import Moran, Moran_BV, Moran_Local, Moran_Local_BV
from libpysal.weights import Queen, lag_spatial
from scipy import stats

st.set_page_config(page_title="BDG Parte 2", page_icon="🌱", layout="wide")

ROOT = Path(__file__).parent
DATA, SQLF = ROOT / "data", ROOT / "sql" / "queries.sql"
PERM = 999
EXCLUIR = ["4300001", "4300002", "5101837"]  # lagoas RS + Boa Esperança do Norte (MT)
LEVELS = {"mun": ("cd_mun", "nm_mun", "Município"),
          "micro": ("cd_micro", "nm_micro", "Região Imediata"),
          "meso": ("cd_meso", "nm_meso", "Região Intermediária")}
REGIAO = {**dict.fromkeys(["AC", "AM", "AP", "PA", "RO", "RR", "TO"], "Norte"),
          **dict.fromkeys(["AL", "BA", "CE", "MA", "PB", "PE", "PI", "RN", "SE"], "Nordeste"),
          **dict.fromkeys(["DF", "GO", "MS", "MT"], "Centro-Oeste"),
          **dict.fromkeys(["ES", "MG", "RJ", "SP"], "Sudeste"),
          **dict.fromkeys(["PR", "RS", "SC"], "Sul")}
VARS = {"pct_flavio": "% Flávio Bolsonaro", "pct_lula": "% Lula", "margem_pp": "Margem Lula − Flávio (p.p.)",
        "pct_area_soja": "% da área do município com soja", "soja_prod_t": "Produção de soja (t)",
        "soja_valor_mil": "Valor da produção de soja (mil R$)", "soja_rend_kg_ha": "Rendimento (kg/ha)",
        "pct_evangelicos": "% evangélicos", "pct_catolicos": "% católicos", "pct_sem_religiao": "% sem religião",
        "dif_evang_cat_pp": "Evangélicos − católicos (p.p.)"}
COVS = {"populacao": "População", "renda_media": "Renda média", "pct_superior": "% ensino superior",
        "pct_evangelicos": "% evangélicos", "pct_urbana": "% população urbana"}
ANO_PAM = 2025  # ano da PAM carregado no banco (ajuste)
CRS_PLOT = 5880  # SIRGAS 2000 / Brazil Polyconic: Brasil menos 'esticado' que lat/lon
VIT_COR = {"LULA_MAIS": "#c0282d", "LULA_MENOS": "#e8806f", "FLAVIO_MAIS": "#173f8a", "FLAVIO_MENOS": "#8e8fc4"}
VIT_ROT = {"LULA_MENOS": "Lula (PT) – menos de 50,01%", "FLAVIO_MENOS": "Flávio (PL) – menos de 50,01%",
           "LULA_MAIS": "Lula (PT) – 50,01% ou mais", "FLAVIO_MAIS": "Flávio (PL) – 50,01% ou mais"}
# limites inferiores das 10 classes de área plantada (ha), ~log (razão ≈ 3,3), como no mapa de referência
SOJA_EDGES = [0, 10.3, 43.8, 152.8, 507, 1660, 5406, 17600, 57210, 186100]
SOJA_COR = ["#e3e3e3", "#cde8e3", "#afdad2", "#8cc8be", "#68b1a6", "#4a9a8f", "#2f8076", "#1f6a61", "#12544d", "#0a3f3a"]
SEM_DADO = "#bdbdbd"
MAPAS_SOJA = {  # classes ~logarítmicas (razão ≈ 3); "zero" = classe própria "Sem produção"
    "Área plantada (ha)": dict(col="soja_area_plantada_ha", edges=SOJA_EDGES, zero=False, rampa=("#cde8e3", "#0a3f3a"),
                               titulo="Área plantada de soja por município", leg="Área plantada (ha)",
                               arq="area_plantada_soja.png"),
    "Produção (toneladas)": dict(col="soja_prod_t", edges=[0, 0.001, 30, 100, 300, 1000, 3000, 10000, 30000, 100000, 300000, 1000000],
                                 zero=True, rampa=("#e1efc9", "#2b5a1b"), titulo="Produção de soja por município",
                                 leg="Produção (t)", arq="producao_soja_t.png"),
    "Valor da produção (mil R$)": dict(col="soja_valor_mil", edges=[0, 0.001, 100, 300, 1000, 3000, 10000, 30000, 100000, 300000, 1000000, 3000000],
                                       zero=True, rampa=("#fde9c4", "#7a3b00"), titulo="Valor da produção de soja por município",
                                       leg="Valor (mil R$)", arq="valor_producao_soja.png"),
}
PRED_COR = {"CAT_MAIORIA": "#b8801a", "CAT_RELATIVA": "#ecc97d", "EVANG": "#5b3a8c"}
PRED_ROT = {"CAT_MAIORIA": "Católicos ≥ 50% da população", "CAT_RELATIVA": "Católicos > evangélicos (< 50%)",
            "EVANG": "Evangélicos ≥ católicos"}
LISA_COR = {"Alto-Alto": "#d7191c", "Baixo-Baixo": "#2c7bb6", "Baixo-Alto": "#abd9e9",
            "Alto-Baixo": "#fdae61", "Não significativo": "#e8e8e8"}


# ----------------------------------------------------------------- dados
def need_files():
    falta = [f for f in ["base_mun.csv", "base_micro.csv", "base_meso.csv", "municipios.geojson", "estados.geojson"]
             if not (DATA / f).exists()]
    if falta:
        st.error(f"Arquivos ausentes em `data/`: {', '.join(falta)}. Rode os SQLs 05, 06 e 07 (página "
                 "'Engenharia de dados') e salve os arquivos.")
        st.stop()


@st.cache_data
def load_base(level):
    key = LEVELS[level][0]
    df = pd.read_csv(DATA / f"base_{level}.csv", dtype={"cd_mun": str, "cd_micro": str, "cd_meso": str})
    if level == "mun":
        df["regiao"] = df["sigla_uf"].map(REGIAO)
        if "categoria_vitoria" not in df:  # fallback caso o CSV não traga a coluna do SQL 02
            lula = df.votos_lula >= df.votos_flavio
            mais = np.where(lula, df.pct_lula, df.pct_flavio) >= 50.01
            df["categoria_vitoria"] = np.where(lula, "LULA", "FLAVIO") + np.where(mais, "_MAIS", "_MENOS")
            df.loc[df.pct_lula.isna(), "categoria_vitoria"] = np.nan
        if {"pct_catolicos", "pct_evangelicos"} <= set(df.columns):
            df["dif_evang_cat_pp"] = df.pct_evangelicos - df.pct_catolicos
            df["predominancia"] = np.select([df.pct_catolicos >= 50, df.pct_catolicos > df.pct_evangelicos],
                                            ["CAT_MAIORIA", "CAT_RELATIVA"], default="EVANG")
            df.loc[df.pct_catolicos.isna(), "predominancia"] = np.nan
    return df.sort_values(key).reset_index(drop=True)


@st.cache_data
def load_geo():
    g = gpd.read_file(DATA / "municipios.geojson")
    g["cd_mun"] = g["cd_mun"].astype(str)
    return g[["cd_mun", "geometry"]]


@st.cache_resource(show_spinner="Preparando geometrias para os mapas...")
def get_plot_data():
    g = load_geo().merge(load_base("mun"), on="cd_mun", how="left").to_crs(CRS_PLOT)
    est = gpd.read_file(DATA / "estados.geojson").to_crs(CRS_PLOT)
    return g, est


def _n(x):
    return f"{x:,.0f}".replace(",", ".") if (x >= 100 or float(x).is_integer()) else f"{x:.1f}".replace(".", ",")


def labels_of(edges, zero=False):
    e, labs = edges, []
    for i in range(len(e) - 1):
        if zero and i == 0:
            labs.append("Sem produção")
        elif zero and i == 1:
            labs.append(f"> 0 – {_n(e[2])}")
        else:
            labs.append(f"{_n(e[i])} – {_n(e[i+1])}")
    return labs + [f"> {_n(e[-1])}"]


def faixa(s_, edges, zero=False):
    return pd.cut(s_, bins=list(edges) + [np.inf], labels=labels_of(edges, zero), right=False)


def soja_labels():
    return labels_of(SOJA_EDGES)


def faixa_ha(s_):
    return faixa(s_, SOJA_EDGES)


def pal(n, rampa):
    cm = LinearSegmentedColormap.from_list("r", list(rampa))
    return ["#e3e3e3"] + [to_hex(cm(i / max(n - 2, 1))) for i in range(n - 1)]


def analise_mun():
    df = load_base("mun")
    return df[df.pct_lula.notna() & ~df.cd_mun.isin(EXCLUIR)].copy()


@st.cache_resource(show_spinner="Montando geometrias...")
def get_gdf(level):
    key = LEVELS[level][0]
    g = load_geo().merge(load_base("mun"), on="cd_mun", how="inner")
    if level == "mun":
        out = g[g.pct_lula.notna() & ~g.cd_mun.isin(EXCLUIR)]
    else:
        d = g[[key, "geometry"]].dissolve(by=key).reset_index()
        out = d.merge(load_base(level), on=key)
    return gpd.GeoDataFrame(out.sort_values(key).reset_index(drop=True))


@st.cache_resource(show_spinner="Construindo matriz de vizinhança (Queen)...")
def get_w(level):
    w = Queen.from_dataframe(get_gdf(level), silence_warnings=True)
    w.transform = "r"
    return w


@st.cache_data(show_spinner=False)
def geojson_of(level):
    key = LEVELS[level][0]
    return json.loads(get_gdf(level)[[key, "geometry"]].set_index(key).to_json())


# ------------------------------------------------------------- estatística
@st.cache_data(show_spinner=False)
def global_moran(level, x, y=None):
    g, w = get_gdf(level), get_w(level)
    np.random.seed(42)
    xv = g[x].to_numpy(float)
    m = Moran(xv, w, permutations=PERM) if y is None else Moran_BV(xv, g[y].to_numpy(float), w, permutations=PERM)
    return float(m.I), float(m.p_sim)


@st.cache_data(show_spinner="Calculando LISA (permutações)...")
def lisa(level, x, y=None):
    g, w = get_gdf(level), get_w(level)
    xv = g[x].to_numpy(float)
    if y is None:
        m = Moran_Local(xv, w, permutations=PERM, keep_simulations=False, seed=42)
    else:
        m = Moran_Local_BV(xv, g[y].to_numpy(float), w, permutations=PERM, keep_simulations=False, seed=42)
    lab = np.array(["Alto-Alto", "Baixo-Alto", "Baixo-Baixo", "Alto-Baixo"])[m.q - 1]
    return np.where(m.p_sim < 0.05, lab, "Não significativo")


def corr_row(d, a, b):
    d = d[[a, b]].dropna()
    r, pr = stats.pearsonr(d[a], d[b])
    rho, ps = stats.spearmanr(d[a], d[b])
    return {"n": len(d), "Pearson": r, "p (Pearson)": pr, "Spearman": rho, "p (Spearman)": ps}


def classes_soja(s):
    bins = [-0.001, 0, 1, 5, 15, 30, 1000]
    labels = ["Não produz", "0–1%", "1–5%", "5–15%", "15–30%", ">30%"]
    return pd.cut(s, bins=bins, labels=labels)


def wmean(d, col, w="votos_validos"):
    return np.average(d[col], weights=d[w]) if len(d) else np.nan


# ------------------------------------------------------------------ mapas
def map_fig(level, col, title, scale="Viridis", mid=None, cap=None, lisa_labels=None):
    key, nome, _ = LEVELS[level]
    g = pd.DataFrame(get_gdf(level).drop(columns="geometry"))
    kw = dict(geojson=geojson_of(level), locations=key, hover_name=nome, center={"lat": -14, "lon": -53},
              zoom=3.1, opacity=0.9, map_style="carto-positron", height=640, title=title)
    if lisa_labels is not None:
        g["cluster"] = lisa_labels
        fig = px.choropleth_map(g, color="cluster", color_discrete_map=LISA_COR,
                                category_orders={"cluster": list(LISA_COR)}, **kw)
    else:
        rng = (g[col].min(), g[col].quantile(cap)) if cap else None
        fig = px.choropleth_map(g, color=col, color_continuous_scale=scale, range_color=rng,
                                color_continuous_midpoint=mid, labels=VARS, **kw)
    fig.update_traces(marker_line_width=0.1 if level == "mun" else 0.4)
    fig.update_layout(margin=dict(l=0, r=0, t=40, b=0))
    return fig


# ------------------------------------------------------------------ SQL
def sql_blocks():
    txt = SQLF.read_text(encoding="utf-8")
    out = []
    for p in re.split(r"^-- @@ ", txt, flags=re.M)[1:]:
        head, _, body = p.partition("\n")
        out.append((head.strip(), body.strip()))
    return out


# ================================================================ PÁGINAS
def pg_intro():
    st.title("🌱 Soja, religião e voto presidencial: padrões espaciais nos municípios")
    st.caption("Trabalho Prático – Parte 2 · Bancos de Dados Geográficos e Ciência de Dados Geoespaciais · DCC/UFMG")
    st.markdown("""
### Motivação e escopo
Este projeto investiga duas dimensões territoriais que podem estar associadas aos resultados eleitorais: **a presença da soja**,
como atividade agrícola específica, e **a composição religiosa** dos municípios, com foco nas proporções de evangélicos e católicos.
A soja não representa todo o agronegócio, e as duas dimensões não são tratadas como causas isoladas do voto.

A dimensão religiosa foi motivada pela visibilidade de símbolos e lideranças religiosas no debate eleitoral de 2026, incluindo uma
controvérsia pública sobre uma alegação relacionada a Nossa Senhora Aparecida.

### Perguntas de pesquisa
1. Onde a soja está concentrada no Brasil e como varia a intensidade da atividade entre municípios?
2. A intensidade da soja se associa aos percentuais de votos de Lula e Flávio Bolsonaro? A associação varia por UF ou região?
3. A composição religiosa municipal se associa aos percentuais de votação? Os padrões diferem entre evangélicos, católicos e pessoas sem religião?
4. A associação entre soja e voto muda quando se consideram religião e efeitos fixos de UF?
5. Existem agrupamentos espaciais da soja, da votação e da composição religiosa?

### Dados e métodos
Resultados eleitorais agregados por município (TSE), produção agrícola (PAM/IBGE), dados religiosos do Censo 2022 (IBGE/SIDRA)
e malhas municipais. São utilizadas estatísticas descritivas, correlações de Pearson e Spearman, comparações por faixas,
modelos OLS/WLS com erros robustos e efeitos fixos de UF, além de Moran global, Moran bivariado e LISA.

**Limite de interpretação:** os resultados são municipais. Não permitem inferir como produtores, evangélicos ou católicos individuais votaram,
nem demonstram causalidade.
""")
    d = analise_mun()
    c = st.columns(4)
    c[0].metric("Municípios analisados", f"{len(d):,}".replace(",", "."))
    c[1].metric("Produtores de soja", f"{int(d.produz_soja.sum()):,}".replace(",", "."))
    c[2].metric("Votos válidos", f"{d.votos_validos.sum()/1e6:.1f} mi")
    c[3].metric("% nacional Lula | Flávio",
                f"{100*d.votos_lula.sum()/d.votos_validos.sum():.1f}% | {100*d.votos_flavio.sum()/d.votos_validos.sum():.1f}%")
    st.info("Os percentuais nacionais são ponderados pelos votos válidos; as correlações e modelos abaixo usam municípios como unidades de análise.")


def pg_eng():
    st.header("1. Engenharia de dados")
    st.markdown("""
**Fluxo:** CSVs do TSE (≈13 mi de linhas por seção) → notebook Python em blocos (`chunksize`) agrega por
município/candidato → carga no PostgreSQL → SQLs abaixo integram votos, PAM e malha IBGE → exportação de CSVs/GeoJSON
→ este app.

| Fonte | Conteúdo | Nível |
|---|---|---|
| TSE – Dados Abertos | Votos por candidato (Presidente, 1º turno 2026) | Seção → agregado ao município |
| IBGE – PAM (SIDRA 1612) | Área plantada/colhida, produção, rendimento e valor da soja | Município |
| IBGE – Malhas 2025 | Municípios, regiões imediatas e intermediárias | Polígonos (tabelas `munic`, `micro`, `meso`) |
""")
    st.subheader("Decisões metodológicas")
    st.markdown("""
- **Percentual de votos** = votos do candidato ÷ soma dos votos nominais de todos os candidatos no município
  (votos válidos). Brancos e nulos ficam fora.
- **Etapa do notebook:** lê o CSV nacional `votacao_candidato_munzona_2026_BRASIL.csv` em blocos de 200 mil linhas, filtra
  `ANO_ELEICAO=2026`, `NR_TURNO=1`, `DS_CARGO=PRESIDENTE` e agrega por município/candidato (75,5 mil linhas selecionadas →
  69 mil combinações, 12 candidatos). Os 5.757 'municípios' incluem o exterior (`ZZ`), que não tem polígono e é excluído no SQL 01.
- **Candidatos** identificados por número (13 = Lula, 22 = Flávio Bolsonaro).
- **Vencedor:** município 'vencido' por quem tem mais votos; categoria ≥ 50,01% ou < 50,01% dos válidos.
- **Voto em trânsito:** se a coluna `ST_VOTO_EM_TRANSITO` existir no CSV, o notebook descarta linhas marcadas como voto em trânsito. Os votos desse tipo não pertencem
  ao município de apuração.
- **Soja ausente = 0.** A PAM só lista quem produz. Municípios sem registro entram com 0 e são separados pela
  variável `produz_soja`.
- **Intensidade de soja** = `pct_area_soja` (área plantada ÷ área do município). Controla pelo tamanho do município,
  ao contrário de toneladas absolutas.
- **Agregação em regiões:** somam-se votos, áreas e produção e **recalculam-se** os percentuais; nunca se faz média de
  percentuais municipais.
- **Exclusões da análise:** Lagoa dos Patos e Lagoa Mirim (4300001/4300002, sem votos nem Censo) e **Boa Esperança do
  Norte/MT (5101837)**, criado em 2025 a partir de Sorriso e Nova Ubiratã: sua soja ainda é contabilizada nos municípios de
  origem na PAM, o que a registraria como 0 por artefato.
""")
    st.subheader("SQLs utilizados (na íntegra)")
    st.caption("O conteúdo abaixo é lido diretamente de `sql/queries.sql`.")
    if SQLF.exists():
        for head, body in sql_blocks():
            with st.expander(head, expanded=False):
                st.code(body, language="sql")
        st.download_button("⬇️ Baixar queries.sql", SQLF.read_text(encoding="utf-8"), "queries.sql")
    st.subheader("Dicionário da base municipal")
    st.dataframe(pd.DataFrame([
        ("cd_mun, nm_mun, sigla_uf", "Código IBGE (7 díg.), nome e UF"),
        ("cd_micro / cd_meso", "Região Geográfica Imediata / Intermediária (IBGE)"),
        ("area_km2", "Área calculada no PostGIS sobre `geography`"),
        ("votos_validos, votos_lula, votos_flavio", "Votos nominais (1º turno)"),
        ("pct_lula, pct_flavio, pct_outros", "% sobre votos válidos"),
        ("margem_pp", "pct_lula − pct_flavio (positivo = vantagem de Lula)"),
        ("soja_area_plantada_ha, soja_prod_t, soja_valor_mil, soja_rend_kg_ha", "Indicadores PAM"),
        ("pct_area_soja", "Área plantada de soja ÷ área do município × 100"),
        ("produz_soja", "Verdadeiro se área plantada > 0"),
        ("populacao, pct_catolicos, pct_evangelicos, pct_sem_religiao", "Censo 2022, SIDRA 10198 (bloco 02b)")], columns=["Variável", "Descrição"]),
        hide_index=True, use_container_width=True)


def pg_eda():
    st.header("2. Análise exploratória")
    df = analise_mun()
    raw = load_base("mun")
    st.markdown(f"Base bruta: **{len(raw)}** linhas; analisadas: **{len(df)}** (excluídas lagoas, Boa Esperança do "
                "Norte e linhas sem votos).")
    cols = [c for c in list(VARS) + list(COVS) if c in df.columns]
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Valores faltantes")
        miss = raw.isna().sum().rename("faltantes").to_frame()
        st.dataframe(miss[miss.faltantes > 0], use_container_width=True)
        st.caption("`soja_rend_kg_ha` é nulo onde não há produção; é esperado. Faltantes em votos = lagoas.")
    with c2:
        st.subheader("Estatísticas descritivas")
        st.dataframe(df[cols].describe().T.round(2), use_container_width=True)
    st.subheader("Distribuições")
    v = st.selectbox("Variável", cols, format_func=lambda c: {**VARS, **COVS}[c])
    sub = st.radio("Amostra", ["Todos", "Apenas produtores de soja"], horizontal=True)
    d = df[df.produz_soja] if sub != "Todos" else df
    st.plotly_chart(px.histogram(d, x=v, nbins=60, labels={**VARS, **COVS}, height=380), use_container_width=True)
    st.subheader("Soja: quão concentrada é a produção?")
    p = df[df.produz_soja].sort_values("soja_prod_t", ascending=False)
    p["acum"] = 100 * p.soja_prod_t.cumsum() / p.soja_prod_t.sum()
    st.markdown(f"Dos **{len(p)}** municípios produtores, os 10% maiores respondem por "
                f"**{p.acum.iloc[int(len(p)*0.1)-1]:.0f}%** da produção.")
    st.plotly_chart(px.line(p.reset_index(drop=True), y="acum", labels={"acum": "% acumulado da produção",
                                                                          "index": "Ranking do município"}, height=320),
                    use_container_width=True)


def _finish(fig, ax, titulo, legend_kw, nota):
    ax.set_axis_off()
    fig.text(0.03, 0.03, nota, fontsize=8, ha="left")
    fig.suptitle(titulo, x=0.03, y=0.97, ha="left", fontsize=20, fontweight="bold")
    b = io.BytesIO()
    fig.savefig(b, format="png", dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return b.getvalue()


@st.cache_data(show_spinner="Desenhando mapa de vitórias...")
def png_vitoria():
    g, est = get_plot_data()
    cores = g["categoria_vitoria"].map(VIT_COR).fillna(SEM_DADO)
    fig, ax = plt.subplots(figsize=(9, 9.6))
    g.plot(ax=ax, color=cores, edgecolor="white", linewidth=0.1)
    est.boundary.plot(ax=ax, color="white", linewidth=0.7)
    ordem = ["LULA_MENOS", "FLAVIO_MENOS", "LULA_MAIS", "FLAVIO_MAIS"]
    h = [Patch(facecolor=VIT_COR[k], label=VIT_ROT[k]) for k in ordem] + [Patch(facecolor=SEM_DADO, label="Sem votos")]
    ax.legend(handles=h, loc="lower left", ncol=2, frameon=False, fontsize=9, title="Vitória no município",
              title_fontsize=11, bbox_to_anchor=(0.0, 0.0), alignment="left")
    return _finish(fig, ax, "Voto nos municípios – 1º turno 2026", {}, "FONTE: TSE. Percentual sobre votos válidos.")


@st.cache_data(show_spinner="Desenhando mapa...")
def png_soja(nome):
    cfg = MAPAS_SOJA[nome]
    g, est = get_plot_data()
    v = g[cfg["col"]].where(~g.cd_mun.isin(EXCLUIR))
    labs = labels_of(cfg["edges"], cfg["zero"])
    cores = pal(len(labs), cfg["rampa"])
    idx = faixa(v, cfg["edges"], cfg["zero"]).cat.codes
    fig, ax = plt.subplots(figsize=(9, 9.6))
    g.plot(ax=ax, color=[cores[i] if i >= 0 else SEM_DADO for i in idx], edgecolor="white", linewidth=0.08)
    est.boundary.plot(ax=ax, color="white", linewidth=1.0)
    h = [Patch(facecolor=c, label=l) for c, l in zip(cores, labs)] + [Patch(facecolor=SEM_DADO, label="Sem dado")]
    ax.legend(handles=h, loc="lower left", frameon=False, fontsize=8, title=f"{cfg['leg']} – soja, {ANO_PAM}",
              title_fontsize=10, alignment="left")
    return _finish(fig, ax, cfg["titulo"], {}, f"FONTE: IBGE – PAM {ANO_PAM}. Classes em escala ~logarítmica.")


@st.cache_data(show_spinner="Desenhando mapa...")
def png_pct(col, titulo, c0, c1):
    g, est = get_plot_data()
    q = pd.qcut(g[col].where(~g.cd_mun.isin(EXCLUIR)), 8, duplicates="drop")
    labs = [f"{iv.left:.1f} – {iv.right:.1f}".replace(".", ",") for iv in q.cat.categories]
    cm = LinearSegmentedColormap.from_list("p", [c0, c1])
    cores = [to_hex(cm(i / max(len(labs) - 1, 1))) for i in range(len(labs))]
    fig, ax = plt.subplots(figsize=(9, 9.6))
    g.plot(ax=ax, color=[cores[i] if i >= 0 else SEM_DADO for i in q.cat.codes], edgecolor="white", linewidth=0.08)
    est.boundary.plot(ax=ax, color="white", linewidth=1.0)
    h = [Patch(facecolor=c, label=l) for c, l in zip(cores, labs)] + [Patch(facecolor=SEM_DADO, label="Sem dado")]
    ax.legend(handles=h, loc="lower left", frameon=False, fontsize=8, title="% da população (8 classes por quantis)",
              title_fontsize=10, alignment="left")
    return _finish(fig, ax, titulo, {}, "FONTE: IBGE – Censo 2022 (SIDRA 10198).")


@st.cache_data(show_spinner="Desenhando mapa...")
def png_pred():
    g, est = get_plot_data()
    fig, ax = plt.subplots(figsize=(9, 9.6))
    g.plot(ax=ax, color=g["predominancia"].map(PRED_COR).fillna(SEM_DADO), edgecolor="white", linewidth=0.08)
    est.boundary.plot(ax=ax, color="white", linewidth=1.0)
    h = [Patch(facecolor=PRED_COR[k], label=PRED_ROT[k]) for k in PRED_COR] + [Patch(facecolor=SEM_DADO, label="Sem dado")]
    ax.legend(handles=h, loc="lower left", frameon=False, fontsize=9, title="Predominância religiosa", title_fontsize=11,
              alignment="left")
    return _finish(fig, ax, "Evangélicos × católicos nos municípios", {}, "FONTE: IBGE – Censo 2022 (SIDRA 10198).")


def _show(png, nome, key):
    st.image(png, use_container_width=True)
    st.download_button("⬇️ Baixar PNG", png, nome, "image/png", key=key)


def pg_maps():
    st.header("3. Mapas")
    t1, t2, t3, t4 = st.tabs(["Voto nos municípios", "Soja: área, produção, valor", "Soja × vitória", "Interativos (Plotly)"])
    with t1:
        c = st.columns([3, 2])
        with c[0]:
            _show(png_vitoria(), "voto_nos_municipios.png", "d1")
        with c[1]:
            d = analise_mun()
            t = d.categoria_vitoria.value_counts().reindex(list(VIT_ROT)).rename(VIT_ROT)
            st.markdown("**Municípios por categoria de vitória**")
            st.dataframe(t.rename("municípios").to_frame().assign(**{"%": (100 * t / t.sum()).round(1)}),
                         use_container_width=True)
            st.markdown("**Proporção de municípios x proporção de votos**")
            st.write(f"Lula venceu em **{(d.vencedor=='LULA').mean()*100:.1f}%** dos municípios, mas obteve "
                     f"**{100*d.votos_lula.sum()/d.votos_validos.sum():.1f}%** dos votos válidos: município pequeno "
                     "e grande pesam diferente no mapa e nas urnas.")
        st.caption("Tons escuros: vitória com 50,01% ou mais dos votos válidos; tons claros: vitória com menos que isso. "
                   "Fundo branco e divisas estaduais em branco, como no mapa de referência.")
    with t2:
        nome = st.radio("Indicador", list(MAPAS_SOJA), horizontal=True)
        cfg = MAPAS_SOJA[nome]
        c = st.columns([3, 2])
        with c[0]:
            _show(png_soja(nome), cfg["arq"], "d2")
        with c[1]:
            d = analise_mun()
            top = d.nlargest(10, cfg["col"])[["nm_mun", "sigla_uf", cfg["col"]]]
            share = 100 * top[cfg["col"]].sum() / d[cfg["col"]].sum()
            st.markdown(f"**10 maiores municípios** ({share:.1f}% do total nacional)")
            st.dataframe(top.rename(columns={"nm_mun": "Município", "sigla_uf": "UF", cfg["col"]: cfg["leg"]}),
                         hide_index=True, use_container_width=True)
            st.write(f"Produtores: **{int(d.produz_soja.sum())}** de {len(d)} municípios.")
        st.caption("Classes em escala aproximadamente logarítmica (razão ≈ 3). Área e produção diferem pela produtividade; "
                   "valor e produção diferem pelo preço/safra. Boa Esperança do Norte e as lagoas aparecem como 'sem dado'.")
    with t3:
        d = analise_mun().copy()
        d["faixa"] = faixa_ha(d.soja_area_plantada_ha)
        r = d.groupby(["faixa", "vencedor"], observed=True).size().rename("n").reset_index()
        r["%"] = 100 * r.n / r.groupby("faixa", observed=True).n.transform("sum")
        fig = px.bar(r, x="faixa", y="%", color="vencedor", color_discrete_map={"LULA": "#c0282d", "FLAVIO": "#173f8a"},
                     category_orders={"faixa": soja_labels()}, height=430,
                     labels={"faixa": "Área plantada de soja (ha) – classe", "%": "% dos municípios da classe"})
        st.plotly_chart(fig, use_container_width=True)
        v = d.groupby("faixa", observed=True).apply(lambda g: pd.Series({
            "municípios": len(g), "% votos Lula (pond.)": wmean(g, "pct_lula"),
            "% votos Flávio (pond.)": wmean(g, "pct_flavio")}), include_groups=False).round(1)
        st.dataframe(v, use_container_width=True)
        st.caption("Quem venceu, por classe de área plantada: leitura direta dos dois mapas acima.")
    with t4:
        level = st.radio("Nível", list(LEVELS), format_func=lambda k: LEVELS[k][2], horizontal=True)
        cap = st.slider("Teto da escala de soja (percentil)", 0.80, 1.0, 0.98)
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(map_fig(level, "pct_area_soja", "% da área com soja", "YlGn", cap=cap), use_container_width=True)
            st.plotly_chart(map_fig(level, "pct_lula", "% Lula", "Reds"), use_container_width=True)
        with c2:
            st.plotly_chart(map_fig(level, "margem_pp", "Margem Lula − Flávio (p.p.)", "RdBu_r", mid=0), use_container_width=True)
            st.plotly_chart(map_fig(level, "pct_flavio", "% Flávio Bolsonaro", "Blues"), use_container_width=True)
        st.info("Versão interativa (zoom/hover) para explorar; os mapas estáticos das outras abas são os produtos finais. "
                "Produção: Plotly `choropleth_map` sobre GeoJSON simplificado no PostGIS.")
    with st.expander("Ficha técnica da visualização"):
        st.markdown(f"""
- **Propósito:** mostrar a coincidência espacial entre os cinturões de soja e a geografia do voto.
- **Plano de fundo:** branco, sem mapa-base, para que a cor carregue toda a informação; divisas estaduais em branco.
- **Parâmetros:** vitória – 2 matizes (vermelho/azul) × 2 intensidades (≥ ou < 50,01%); soja – sequencial verde-azulado,
  10 classes log, cinza = menor classe/sem dado. Projeção SIRGAS 2000 Polyconic (EPSG:{CRS_PLOT}).
- **Produção:** GeoPandas + Matplotlib (estáticos, 170 dpi) e Plotly (interativos); geometria simplificada no PostGIS.
""")


def pg_corr():
    st.header("4. Correlação com a produção de soja")
    df = analise_mun()
    xs = [c for c in ["pct_area_soja", "soja_prod_t", "soja_valor_mil", "soja_rend_kg_ha"] if c in df]
    ys = ["pct_flavio", "pct_lula", "margem_pp"]
    amostra = st.radio("Amostra", ["Todos os municípios", "Somente produtores de soja"], horizontal=True)
    d = df[df.produz_soja] if amostra.startswith("Somente") else df
    rows = [{"Soja": VARS[x], "Voto": VARS[y], **corr_row(d, x, y)} for x in xs for y in ys]
    t = pd.DataFrame(rows)
    st.dataframe(t.style.format({"Pearson": "{:.3f}", "Spearman": "{:.3f}", "p (Pearson)": "{:.1e}",
                                 "p (Spearman)": "{:.1e}"}), hide_index=True, use_container_width=True)
    st.caption("Spearman é a medida principal (distribuições assimétricas e muitos zeros). Com milhares de "
               "observações quase tudo é 'significativo'; observe o **tamanho** da correlação.")
    st.subheader("Relação entre intensidade de soja e votos")
    y = st.selectbox("Voto", ys, format_func=lambda c: VARS[c])
    s = d.sample(min(len(d), 4000), random_state=1)
    fig = px.scatter(s, x="pct_area_soja", y=y, color="regiao", opacity=0.45, trendline="lowess",
                     trendline_scope="overall", hover_name="nm_mun", labels=VARS, height=480,
                     hover_data=["sigla_uf"])
    st.plotly_chart(fig, use_container_width=True)
    st.subheader("Por faixa de intensidade de soja")
    df = df.assign(faixa=classes_soja(df.pct_area_soja))
    fx = df.groupby("faixa", observed=True).apply(lambda g: pd.Series({
        "municípios": len(g), "pct_flavio (média simples)": g.pct_flavio.mean(),
        "pct_flavio (ponderada por votos)": wmean(g, "pct_flavio"),
        "pct_lula (média simples)": g.pct_lula.mean(), "pct_lula (ponderada por votos)": wmean(g, "pct_lula")}),
        include_groups=False).round(2)
    st.dataframe(fx, use_container_width=True)
    mm = df.melt(id_vars="faixa", value_vars=["pct_flavio", "pct_lula"], var_name="candidato", value_name="pct")
    st.plotly_chart(px.box(mm, x="faixa", y="pct", color="candidato", labels={"pct": "% votos", "faixa": "% de área com soja"},
                           color_discrete_map={"pct_flavio": "#2c7bb6", "pct_lula": "#d7191c"}, height=450),
                    use_container_width=True)
    st.subheader("A correlação é espacializada? Spearman por UF")
    ufs = []
    for uf, g in df.groupby("sigla_uf"):
        if g.produz_soja.sum() >= 15:
            ufs.append({"UF": uf, "n_produtores": int(g.produz_soja.sum()),
                        "Spearman % Flávio": stats.spearmanr(g.pct_area_soja, g.pct_flavio)[0],
                        "Spearman % Lula": stats.spearmanr(g.pct_area_soja, g.pct_lula)[0]})
    u = pd.DataFrame(ufs).sort_values("Spearman % Flávio")
    st.plotly_chart(px.bar(u.melt(id_vars=["UF", "n_produtores"], var_name="voto", value_name="rho"), x="UF", y="rho",
                           color="voto", barmode="group", height=400,
                           color_discrete_map={"Spearman % Flávio": "#2c7bb6", "Spearman % Lula": "#d7191c"}),
                    use_container_width=True)
    st.caption("UFs com ≥ 15 municípios produtores. Se o sinal muda entre UFs, a relação nacional é em parte "
               "efeito de composição regional.")
    st.subheader("Regressão (OLS, erros robustos HC3)")
    ponder = st.checkbox("Ponderar por votos válidos (WLS)")
    ycol = st.selectbox("Variável dependente", ys, key="reg_y", format_func=lambda c: VARS[c])
    covs = [c for c in COVS if c in df]
    dd = df.dropna(subset=covs + [ycol]) if covs else df
    cv = " + ".join([f"np.log({c})" if c == "populacao" else c for c in covs])
    specs = {"M1: só soja": "pct_area_soja", "M2: + efeitos fixos de UF": "pct_area_soja + C(sigla_uf)"}
    if covs:
        specs["M3: + covariáveis do Censo + UF"] = f"pct_area_soja + {cv} + C(sigla_uf)"
    out = []
    for nome, rhs in specs.items():
        kw = dict(weights=dd.votos_validos) if ponder else {}
        m = (smf.wls if ponder else smf.ols)(f"{ycol} ~ {rhs}", dd, **kw).fit(cov_type="HC3")
        ci = m.conf_int().loc["pct_area_soja"]
        out.append({"Modelo": nome, "Coef. soja (p.p. por p.p.)": m.params["pct_area_soja"],
                    "IC95 inf.": ci[0], "IC95 sup.": ci[1], "p": m.pvalues["pct_area_soja"], "R²": m.rsquared,
                    "n": int(m.nobs)})
    st.dataframe(pd.DataFrame(out).style.format({c: "{:.3f}" for c in ["Coef. soja (p.p. por p.p.)", "IC95 inf.", "IC95 sup.", "R²"]}
                                                | {"p": "{:.1e}"}), hide_index=True, use_container_width=True)
    if not covs:
        st.info("Adicione covariáveis do Censo (população, renda, escolaridade, religião) à base municipal (ver SQL 02) "
                "para habilitar o modelo M3: elas são os principais confundidores.")
    st.caption("Coeficiente de M1 para M2: quanto cai ao incluir UF indica quanto da associação é diferença entre estados.")


def pg_spatial():
    st.header("5. Autocorrelação espacial")
    st.markdown("""
Vizinhança **Queen** (municípios/regiões que compartilham fronteira ou vértice), pesos padronizados por linha.
**Moran I global** mede se valores semelhantes ficam próximos (>0 agrupamento, ≈0 aleatório). O **Moran bivariado**
correlaciona a soja de uma área com o voto médio dos vizinhos. **LISA** localiza os clusters
(Alto-Alto, Baixo-Baixo e outliers). Inferência por {} permutações, p < 0,05.
""".format(PERM))
    vars3 = ["pct_flavio", "pct_lula", "pct_area_soja"]
    st.subheader("Moran global: município × região imediata × região intermediária")
    if st.button("Calcular/atualizar tabela completa"):
        rows = []
        for lv in LEVELS:
            for v in vars3:
                I, p = global_moran(lv, v)
                rows.append({"Nível": LEVELS[lv][2], "Variável": VARS[v], "Tipo": "Univariado", "I": I, "p": p,
                             "n": len(get_gdf(lv))})
            for y in ["pct_flavio", "pct_lula"]:
                I, p = global_moran(lv, "pct_area_soja", y)
                rows.append({"Nível": LEVELS[lv][2], "Variável": f"soja → vizinhos {VARS[y]}", "Tipo": "Bivariado",
                             "I": I, "p": p, "n": len(get_gdf(lv))})
        st.session_state["moran_tab"] = pd.DataFrame(rows)
    if "moran_tab" in st.session_state:
        t = st.session_state["moran_tab"]
        st.dataframe(t.style.format({"I": "{:.3f}", "p": "{:.3f}"}), hide_index=True, use_container_width=True)
        st.plotly_chart(px.bar(t, x="Variável", y="I", color="Nível", barmode="group", height=420), use_container_width=True)
        st.caption("Compare o I entre níveis: queda ao agregar indica que parte do padrão é local; aumento, que é regional. "
                   "Mudança de resultado com a escala é o problema da unidade de área modificável (MAUP).")
    st.subheader("Mapa de clusters (LISA)")
    c1, c2 = st.columns(2)
    level = c1.radio("Nível", list(LEVELS), format_func=lambda k: LEVELS[k][2], horizontal=True, key="l_lv")
    OPS = {"% Flávio (univariado)": ("pct_flavio", None), "% Lula (univariado)": ("pct_lula", None),
           "% de área com soja (univariado)": ("pct_area_soja", None),
           "Soja → % Flávio dos vizinhos (bivariado)": ("pct_area_soja", "pct_flavio"),
           "Soja → % Lula dos vizinhos (bivariado)": ("pct_area_soja", "pct_lula")}
    g0 = get_gdf(level)
    if "pct_evangelicos" in g0 and g0.pct_evangelicos.notna().any():
        OPS.update({"% evangélicos (univariado)": ("pct_evangelicos", None),
                    "% católicos (univariado)": ("pct_catolicos", None),
                    "Evangélicos → % Flávio dos vizinhos (bivariado)": ("pct_evangelicos", "pct_flavio"),
                    "Evangélicos → % Lula dos vizinhos (bivariado)": ("pct_evangelicos", "pct_lula"),
                    "Católicos → % Lula dos vizinhos (bivariado)": ("pct_catolicos", "pct_lula")})
    op = c2.selectbox("Análise", list(OPS))
    x, y = OPS[op]
    lab = lisa(level, x, y)
    st.plotly_chart(map_fig(level, None, f"LISA – {op}", lisa_labels=lab), use_container_width=True)
    cont = pd.Series(lab).value_counts().rename("áreas").to_frame()
    cont["%"] = (100 * cont["áreas"] / len(lab)).round(1)
    st.dataframe(cont, use_container_width=True)
    if y:
        st.caption("Alto-Alto = soja alta cercada por vizinhos com voto alto no candidato; Baixo-Baixo = soja baixa e vizinhos com voto baixo.")
    st.subheader("Diagrama de dispersão de Moran")
    g, w = get_gdf(level), get_w(level)
    z = (g[x] - g[x].mean()) / g[x].std()
    zl = lag_spatial(w, ((g[y] - g[y].mean()) / g[y].std()).to_numpy() if y else z.to_numpy())
    dm = pd.DataFrame({"z": z, "lag": zl, "nome": g[LEVELS[level][1]]})
    st.plotly_chart(px.scatter(dm, x="z", y="lag", hover_name="nome", opacity=0.4, trendline="ols", height=420,
                               labels={"z": f"{x} (padronizado)", "lag": "defasagem espacial (padronizada)"}),
                    use_container_width=True)


def pg_relig():
    st.header("6. Religião: composição religiosa, soja e votação")
    df = analise_mun()
    if "pct_evangelicos" not in df or df.pct_evangelicos.isna().all():
        st.warning("A base não tem as colunas de religião. Rode o bloco 02b e os blocos 03/04 do `queries.sql` e reexporte os CSVs.")
        return

    st.markdown("""
Os percentuais religiosos vêm do **Censo Demográfico 2022 (IBGE/SIDRA, tabela 10198)**. 

**Motivação jornalística:** em setembro de 2026 circulou uma alegação sobre uma possível retirada do título de Padroeira do Brasil
de Nossa Senhora Aparecida.

Leituras de contexto:
- [CNN Brasil — Entenda a polêmica envolvendo Flávio Bolsonaro e Nossa Senhora Aparecida](https://www.cnnbrasil.com.br/eleicoes/entenda-polemica-envolvendo-flavio-bolsonaro-e-nossa-senhora-aparecida/)
- [Folha de S.Paulo — Flávio e Lula acenam a católicos com Nossa Senhora após polêmica no primeiro turno](https://www1.folha.uol.com.br/poder/2026/10/flavio-e-lula-acenam-a-catolicos-com-nossa-senhora-apos-polemica-no-primeiro-turno.shtml)

### Perguntas desta seção
- A proporção de evangélicos e católicos se associa aos percentuais municipais de votação?
- A associação varia entre estados e faixas de intensidade da soja?
- O coeficiente da soja muda quando religião e UF entram no modelo?
""")
    t1, t2, t3, t4 = st.tabs(["Mapas", "Religião × voto", "Modelos ajustados", "Soja × religião"])
    ys = ["pct_flavio", "pct_lula", "margem_pp"]

    with t1:
        c = st.columns(2)
        with c[0]:
            _show(png_pct("pct_evangelicos", "Evangélicos nos municípios", "#ebe3f5", "#2e1a5e"), "pct_evangelicos.png", "r1")
        with c[1]:
            _show(png_pct("pct_catolicos", "Católicos nos municípios", "#fbefd0", "#7a4b00"), "pct_catolicos.png", "r2")
        _show(png_pred(), "predominancia_religiosa.png", "r3")
        st.caption("Os mapas mostram padrões territoriais; não permitem inferir a religião ou o voto de indivíduos.")

    with t2:
        xs = [c for c in ["pct_evangelicos", "pct_catolicos", "pct_sem_religiao", "dif_evang_cat_pp"] if c in df]
        t = pd.DataFrame([{"Variável religiosa": VARS[x], "Voto": VARS[y], **corr_row(df, x, y)}
                          for x in xs for y in ys])
        st.subheader("Correlação de Pearson e Spearman")
        st.dataframe(t.style.format({"Pearson": "{:.3f}", "Spearman": "{:.3f}",
                                     "p (Pearson)": "{:.1e}", "p (Spearman)": "{:.1e}"}),
                     hide_index=True, use_container_width=True)
        st.caption("Priorize o tamanho e o sinal de Spearman; com milhares de municípios, valores-p pequenos não significam associação forte.")

        c1, c2 = st.columns(2)
        with c1:
            x = st.selectbox("Composição religiosa", xs, format_func=lambda c: VARS[c], key="rel_x")
        with c2:
            y = st.selectbox("Percentual de voto", ys, format_func=lambda c: VARS[c], key="rel_y")
        s_ = df[[x, y, "regiao", "nm_mun", "sigla_uf"]].dropna()
        s_ = s_.sample(min(len(s_), 4000), random_state=1)
        st.plotly_chart(px.scatter(s_, x=x, y=y, color="regiao", opacity=0.45, trendline="lowess",
                                   trendline_scope="overall", hover_name="nm_mun", hover_data=["sigla_uf"],
                                   labels=VARS, height=470, title=f"{VARS[x]} × {VARS[y]} por município"),
                        use_container_width=True)

        st.subheader("Votação ponderada por faixas de composição religiosa")
        cat_opts = [x for x in ["pct_evangelicos", "pct_catolicos", "pct_sem_religiao"] if x in df]
        relig_x = st.selectbox("Agrupar municípios por", cat_opts, format_func=lambda c: VARS[c], key="rel_bins")
        bins = [-0.001, 10, 20, 30, 40, 50, 100.001]
        labels = ["<10%", "10–20%", "20–30%", "30–40%", "40–50%", "≥50%"]
        d = df.copy()
        d["faixa_religiosa"] = pd.cut(d[relig_x], bins=bins, labels=labels, right=False)
        f = d.groupby("faixa_religiosa", observed=True).apply(lambda g: pd.Series({
            "Municípios": len(g), "% Flávio (ponderado)": wmean(g, "pct_flavio"),
            "% Lula (ponderado)": wmean(g, "pct_lula")}), include_groups=False).reset_index()
        st.dataframe(f.round(2), hide_index=True, use_container_width=True)
        plot_f = f.melt(id_vars=["faixa_religiosa", "Municípios"],
                        value_vars=["% Flávio (ponderado)", "% Lula (ponderado)"],
                        var_name="Candidato", value_name="% dos votos válidos")
        st.plotly_chart(px.bar(plot_f, x="faixa_religiosa", y="% dos votos válidos", color="Candidato",
                               barmode="group", hover_data=["Municípios"], height=400,
                               labels={"faixa_religiosa": VARS[relig_x]},
                               color_discrete_map={"% Flávio (ponderado)": "#2c7bb6",
                                                   "% Lula (ponderado)": "#d7191c"}),
                        use_container_width=True)

        st.subheader("Votação por predominância religiosa")
        p = df.dropna(subset=["predominancia"]).groupby("predominancia").apply(lambda g: pd.Series({
            "Municípios": len(g), "Flávio vence (% municípios)": 100 * (g.vencedor == "FLAVIO").mean(),
            "Lula vence (% municípios)": 100 * (g.vencedor == "LULA").mean(),
            "% Flávio (ponderado)": wmean(g, "pct_flavio"), "% Lula (ponderado)": wmean(g, "pct_lula")}),
            include_groups=False).round(1)
        st.dataframe(p.rename(index=PRED_ROT), use_container_width=True)

        st.subheader("A associação varia por UF?")
        u_rows = []
        for uf, g in df.groupby("sigla_uf"):
            for x in ["pct_evangelicos", "pct_catolicos"]:
                for y in ["pct_flavio", "pct_lula"]:
                    q = g[[x, y]].dropna()
                    if len(q) >= 15 and q[x].nunique() > 1 and q[y].nunique() > 1:
                        u_rows.append({"UF": uf, "Religião": VARS[x], "Voto": VARS[y],
                                       "rho": stats.spearmanr(q[x], q[y]).statistic, "n": len(q)})
        u = pd.DataFrame(u_rows)
        if not u.empty:
            u["Série"] = u["Religião"] + " × " + u["Voto"]
            st.plotly_chart(px.bar(u, x="UF", y="rho", color="Série", barmode="group",
                                   hover_data=["n"], height=440, title="Spearman por UF"),
                            use_container_width=True)
        st.caption("Comparações por faixa e predominância são descritivas e ponderadas por votos válidos. Não identificam o voto individual de grupos religiosos.")

    with t3:
        st.markdown("Modelos OLS/WLS com erros-padrão robustos HC3. Os coeficientes são associações condicionais; não devem ser interpretados causalmente.")
        yv = st.selectbox("Variável dependente", ys, format_func=lambda c: VARS[c], key="rel_m")
        pond = st.checkbox("Ponderar por votos válidos (WLS)", key="rel_w")
        needed = ["pct_evangelicos", "pct_catolicos", "pct_area_soja", yv, "votos_validos", "sigla_uf"]
        dd = df.dropna(subset=[c for c in needed if c in df]).copy()
        rel = "pct_evangelicos + pct_catolicos"
        specs = {"R1: só religião": rel, "R2: só soja": "pct_area_soja",
                 "R3: soja + religião": f"pct_area_soja + {rel}",
                 "R4: soja + religião + UF": f"pct_area_soja + {rel} + C(sigla_uf)"}
        if "populacao" in dd and dd.populacao.gt(0).all():
            specs["R5: R4 + log(população)"] = f"pct_area_soja + {rel} + C(sigla_uf) + np.log(populacao)"
        out = []
        for nome, rhs in specs.items():
            kw = dict(weights=dd.votos_validos) if pond else {}
            m = (smf.wls if pond else smf.ols)(f"{yv} ~ {rhs}", dd, **kw).fit(cov_type="HC3")
            row = {"Modelo": nome, "R²": m.rsquared, "n": int(m.nobs)}
            for term in ["pct_area_soja", "pct_evangelicos", "pct_catolicos"]:
                if term in m.params.index:
                    ci = m.conf_int().loc[term]
                    row[f"{VARS[term]} — coef."] = round(m.params[term], 4)
                    row[f"{VARS[term]} — p"] = f"{m.pvalues[term]:.2e}"
                    row[f"{VARS[term]} — IC95%"] = f"[{ci[0]:.3f}; {ci[1]:.3f}]"
            out.append(row)
        st.dataframe(pd.DataFrame(out), hide_index=True, use_container_width=True)
        st.caption("Compare R2 com R3–R5 para avaliar quanto o coeficiente da soja muda após incluir religião e UF. Percentuais religiosos podem ser correlacionados entre si.")
        if "pct_sem_religiao" in df:
            st.info("O percentual sem religião aparece nas análises descritivas, mas não foi incluído junto com evangélicos e católicos para reduzir problemas de colinearidade entre proporções.")

    with t4:
        rel_vars = [x for x in ["pct_evangelicos", "pct_catolicos", "pct_sem_religiao"] if x in df]
        r = {x: corr_row(df, "pct_area_soja", x) for x in rel_vars}
        st.subheader("Associação entre intensidade de soja e composição religiosa")
        st.dataframe(pd.DataFrame([{"Variável religiosa": VARS[x], **r[x]} for x in rel_vars]).style.format(
            {"Pearson": "{:.3f}", "Spearman": "{:.3f}", "p (Pearson)": "{:.1e}", "p (Spearman)": "{:.1e}"}),
            hide_index=True, use_container_width=True)
        d = df.assign(faixa=classes_soja(df.pct_area_soja))
        g = d.dropna(subset=["predominancia"]).groupby(["faixa", "predominancia"], observed=True).apply(lambda q: pd.Series({
            "n": len(q), "% Flávio": wmean(q, "pct_flavio"), "% Lula": wmean(q, "pct_lula")}),
            include_groups=False).reset_index()
        g["predominancia"] = g.predominancia.map(PRED_ROT)
        cand = st.selectbox("Candidato para comparar por soja e predominância religiosa",
                            ["% Flávio", "% Lula"], key="soja_relig_cand")
        st.plotly_chart(px.bar(g, x="faixa", y=cand, color="predominancia", barmode="group", height=430,
                               hover_data=["n"], color_discrete_map={PRED_ROT[k]: v for k, v in PRED_COR.items()},
                               labels={cand: f"{cand} (ponderado)", "faixa": "% de área com soja"},
                               title=f"{cand} por intensidade de soja e predominância religiosa"),
                        use_container_width=True)
        st.caption("Compare faixas de soja dentro de cada categoria religiosa e categorias religiosas dentro de cada faixa. A leitura é descritiva, não causal.")


# ================================================================= ROUTER
PAGES = {"Introdução": pg_intro, "1. Engenharia de dados": pg_eng, "2. Exploratória": pg_eda, "3. Mapas": pg_maps,
         "4. Correlação": pg_corr, "5. Autocorrelação espacial": pg_spatial, "6. Religião": pg_relig}
st.sidebar.title("Voto 2026")
sel = st.sidebar.radio("Seções", list(PAGES))
st.sidebar.caption("Dados: TSE, IBGE (PAM, malhas 2025). Percentuais sobre votos válidos.")
if sel != "1. Engenharia de dados":
    need_files()
PAGES[sel]()
