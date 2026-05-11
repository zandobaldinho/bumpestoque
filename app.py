"""
Controle de Produção / Estoque — versão web
Stack: Streamlit + Google Sheets (gspread + google-auth)

Perfis:
- admin_completo: edita tudo (inline + ações em item específico), gerencia itens, fecha semana
- admin_pagamento: vê só itens em débito e lança pagamentos
- visualizador: vê tudo em modo leitura
"""

import streamlit as st
import pandas as pd
from datetime import datetime
import hashlib
import io
import gspread
from google.oauth2.service_account import Credentials


# ==================================================
# CONFIGURAÇÃO
# ==================================================

WORKSHEET_ESTOQUE = "Estoque"
WORKSHEET_HISTORICO = "Historico"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Para gerar hash de senha nova:
#   import hashlib; print(hashlib.sha256("senha".encode()).hexdigest())
USUARIOS = {
    "guilhermeadmin": {
        "senha_hash": "8a5af9f2c0290c31a9042bc03598a6eb4db340163f4194797554e53c103712ef",
        "perfil": "admin_completo",
        "nome": "Guilherme",
    },
    "abner": {
        "senha_hash": "e19f85647a83be0aa7d9683d8590380928fd3b3be9fedb7dab507df86f2d6102",
        "perfil": "admin_pagamento",
        "nome": "Abner",
    },
    "wagnerbender": {
        "senha_hash": "7e5e2e43c4eb7ab30d26677b8b4132f4ca74d154439489db5a46b477681387f3",
        "perfil": "visualizador",
        "nome": "Wagner Bender",
    },
}

DADOS_PADRAO = {
    "Conjunto Tampa Guia": [
        "Tampa guia", "Gaxeta", "Tampa pó", "Anel grafitado", "O-ring",
    ],
    "Conjunto Tampa Gás": [
        "Tampa gás", "Valvula TR4", "Arruela TR4", "Núcleo TR4", "Porca TR4", "O-ring",
    ],
    "Conjunto Embolo": [
        "Embolo", "Backup", "Viton",
    ],
    "Conjunto Diversos": [
        "Arruela Pressão", "Anel elástico", "Batente", "Chapéu chinês",
        "Mola P", "Mola G", "Porca",
        "Válvula de 1 furo", "Válvula de 3 furo", "Válvula de 2 furo", "Válvula guia",
    ],
}

COLUNAS_ESTOQUE = ["Conjunto", "Item", "Meta", "Real", "Pago", "Status"]
COLUNAS_HISTORICO = ["Data", "Conjunto", "Item", "Tipo", "Valor", "Usuario"]


# ==================================================
# UTILS
# ==================================================

def calcular_status(meta: int, real: int) -> str:
    faltando = meta - real
    if faltando > 0:
        return f"Faltam {faltando}"
    if faltando < 0:
        return f"Sobram {abs(faltando)}"
    return "OK"


def garantir_tipos(df: pd.DataFrame) -> pd.DataFrame:
    for col in ("Meta", "Real", "Pago"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    return df


def agora() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M")


def usuario_atual() -> str:
    return st.session_state.get("usuario", "?")


# ==================================================
# DIAGNÓSTICO DOS SECRETS
# ==================================================

def diagnostico_secrets():
    problemas = []
    if "connections" not in st.secrets or "gsheets" not in st.secrets.get("connections", {}):
        return [
            "A seção `[connections.gsheets]` está faltando nos Secrets.",
            "Vá em Settings → Secrets e cole o bloco completo.",
        ]
    cfg = st.secrets["connections"]["gsheets"]
    if "spreadsheet" not in cfg or not str(cfg.get("spreadsheet", "")).startswith("http"):
        problemas.append("Falta `spreadsheet = \"https://...\"` em `[connections.gsheets]`.")
    if cfg.get("type") != "service_account":
        problemas.append("Falta `type = \"service_account\"` em `[connections.gsheets]`.")
    for campo in ("project_id", "private_key_id", "private_key", "client_email", "client_id"):
        if not cfg.get(campo):
            problemas.append(f"Falta o campo `{campo}` nos Secrets.")
    return problemas


# ==================================================
# GOOGLE SHEETS — via gspread
# ==================================================

@st.cache_resource
def get_gspread_client():
    cfg = dict(st.secrets["connections"]["gsheets"])
    creds_dict = {k: v for k, v in cfg.items() if k not in ("spreadsheet", "worksheet")}
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_spreadsheet():
    url = st.secrets["connections"]["gsheets"]["spreadsheet"]
    return get_gspread_client().open_by_url(url)


def get_or_create_worksheet(nome: str, colunas: list):
    sh = get_spreadsheet()
    try:
        return sh.worksheet(nome)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=nome, rows=200, cols=max(len(colunas), 10))
        ws.append_row(colunas)
        return ws


def ler_worksheet(nome: str, colunas: list) -> pd.DataFrame:
    ws = get_or_create_worksheet(nome, colunas)
    registros = ws.get_all_records()
    if not registros:
        return pd.DataFrame(columns=colunas)
    df = pd.DataFrame(registros)
    for c in colunas:
        if c not in df.columns:
            df[c] = ""
    return df[colunas]


def escrever_worksheet(nome: str, df: pd.DataFrame, colunas: list):
    ws = get_or_create_worksheet(nome, colunas)
    ws.clear()
    valores = [colunas] + df[colunas].astype(str).values.tolist()
    ws.update(valores, value_input_option="USER_ENTERED")


def carregar_estoque() -> pd.DataFrame:
    df = ler_worksheet(WORKSHEET_ESTOQUE, COLUNAS_ESTOQUE)
    if df.empty:
        return inicializar_estoque()
    return garantir_tipos(df)


def carregar_historico() -> pd.DataFrame:
    return ler_worksheet(WORKSHEET_HISTORICO, COLUNAS_HISTORICO)


def salvar_estoque(df: pd.DataFrame):
    escrever_worksheet(WORKSHEET_ESTOQUE, garantir_tipos(df), COLUNAS_ESTOQUE)


def salvar_historico(df: pd.DataFrame):
    escrever_worksheet(WORKSHEET_HISTORICO, df, COLUNAS_HISTORICO)


def inicializar_estoque() -> pd.DataFrame:
    lista = []
    for conjunto, itens in DADOS_PADRAO.items():
        for item in itens:
            lista.append({
                "Conjunto": conjunto, "Item": item,
                "Meta": 0, "Real": 0, "Pago": 0, "Status": "OK",
            })
    df = pd.DataFrame(lista)
    salvar_estoque(df)
    salvar_historico(pd.DataFrame(columns=COLUNAS_HISTORICO))
    return df


def registrar_no_historico(linhas: list):
    if not linhas:
        return
    hist = carregar_historico()
    hist = pd.concat([hist, pd.DataFrame(linhas)], ignore_index=True)
    salvar_historico(hist)


# ==================================================
# FECHAR SEMANA
# ==================================================

def gerar_xlsx_fechamento() -> bytes:
    estoque = carregar_estoque()
    historico = carregar_historico()
    resumo = estoque.groupby("Conjunto").agg(
        Itens=("Item", "count"),
        Meta_Total=("Meta", "sum"),
        Real_Total=("Real", "sum"),
        Pago_Total=("Pago", "sum"),
    ).reset_index()
    resumo["Diferenca"] = resumo["Real_Total"] - resumo["Meta_Total"]
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        estoque.to_excel(writer, sheet_name="Estoque_Final", index=False)
        historico.to_excel(writer, sheet_name="Historico", index=False)
        resumo.to_excel(writer, sheet_name="Resumo", index=False)
    return buffer.getvalue()


def fechar_semana_reset():
    estoque = carregar_estoque()
    estoque["Meta"] = 0
    estoque["Real"] = 0
    estoque["Pago"] = 0
    estoque["Status"] = "OK"
    salvar_estoque(estoque)
    salvar_historico(pd.DataFrame(columns=COLUNAS_HISTORICO))


# ==================================================
# LOGIN
# ==================================================

def autenticar(usuario: str, senha: str):
    if usuario not in USUARIOS:
        return None
    info = USUARIOS[usuario]
    if hashlib.sha256(senha.encode()).hexdigest() != info["senha_hash"]:
        return None
    return info


def tela_login():
    st.markdown("## Controle de Produção / Estoque")
    st.write("Faça login para continuar.")
    with st.form("login_form"):
        usuario = st.text_input("Usuário")
        senha = st.text_input("Senha", type="password")
        enviar = st.form_submit_button("Entrar", type="primary")
    if enviar:
        info = autenticar(usuario.strip(), senha)
        if info is None:
            st.error("Usuário ou senha inválidos.")
        else:
            st.session_state.update({
                "logado": True, "usuario": usuario.strip(),
                "perfil": info["perfil"], "nome": info["nome"],
            })
            st.rerun()


# ==================================================
# COMPONENTES VISUAIS REUTILIZÁVEIS
# ==================================================

def header(df: pd.DataFrame, subtitulo: str = ""):
    """Título, usuário/logout e dashboard de totais."""
    col_t, col_u = st.columns([4, 1])
    with col_t:
        st.markdown("## Controle de Produção / Estoque")
        if subtitulo:
            st.caption(subtitulo)
    with col_u:
        st.write("")
        c1, c2 = st.columns([2, 1])
        c1.markdown(f"**{st.session_state['nome']}**")
        if c2.button("Sair", key="btn_sair"):
            for k in ("logado", "usuario", "perfil", "nome"):
                st.session_state.pop(k, None)
            st.rerun()

    total_itens = len(df)
    total_meta = int(df["Meta"].sum()) if not df.empty else 0
    total_real = int(df["Real"].sum()) if not df.empty else 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Itens", total_itens)
    c2.metric("Meta Total", total_meta)
    c3.metric("Produzido", total_real)
    c4.metric("Diferença", total_real - total_meta)


def aplicar_busca(df: pd.DataFrame, busca: str) -> pd.DataFrame:
    if not busca.strip():
        return df
    return df[df["Item"].str.lower().str.contains(busca.lower())]


def rotulo_item(conjunto: str, item: str) -> str:
    return f"{conjunto}  →  {item}"


def parse_rotulo(rotulo: str):
    if "  →  " not in rotulo:
        return None, None
    c, i = rotulo.split("  →  ", 1)
    return c, i


# ==================================================
# TELA ADMIN COMPLETO
# ==================================================

def tela_admin_completo():
    df = carregar_estoque()
    header(df)

    busca = st.text_input("Buscar item", "", placeholder="Digite parte do nome...")
    df_view = aplicar_busca(df, busca).sort_values(["Conjunto", "Item"]).reset_index(drop=True)

    # ============ Tabela editável (edição inline) ============
    st.markdown("##### Itens — duplo clique numa célula de Meta/Real/Pago para editar")

    editado = st.data_editor(
        df_view,
        use_container_width=True, hide_index=True,
        disabled=["Conjunto", "Item", "Status"],
        column_config={
            "Conjunto": st.column_config.TextColumn(width="medium"),
            "Item": st.column_config.TextColumn(width="medium"),
            "Meta": st.column_config.NumberColumn(width="small", min_value=0, step=1),
            "Real": st.column_config.NumberColumn(width="small", min_value=0, step=1),
            "Pago": st.column_config.NumberColumn(width="small", min_value=0, step=1),
            "Status": st.column_config.TextColumn(width="small"),
        },
        num_rows="fixed",
        height=500,
        key="editor_estoque",
    )

    c1, c2 = st.columns([1, 4])
    if c1.button("Salvar alterações", type="primary", key="btn_salvar_edicoes"):
        salvar_edicoes_inline(df, editado)

    st.divider()

    # ============ Ações em item específico ============
    st.markdown("##### Ações em um item específico")
    st.caption("Para zerar pago, somar produção (incremental) ou excluir item.")

    rotulos = [rotulo_item(r["Conjunto"], r["Item"]) for _, r in df.sort_values(["Conjunto", "Item"]).iterrows()]
    rotulo_sel = st.selectbox(
        "Escolha o item", [""] + rotulos, key="sel_acoes",
        format_func=lambda x: "— selecione —" if x == "" else x,
    )

    if rotulo_sel:
        acoes_item(df, rotulo_sel)

    st.divider()

    # ============ Expanders no rodapé ============
    with st.expander("Adicionar item"):
        adicionar_item_form(df)

    with st.expander("Histórico"):
        mostrar_historico()

    with st.expander("Fechar Semana"):
        fechar_semana_form(df)


def salvar_edicoes_inline(df_original: pd.DataFrame, editado: pd.DataFrame):
    """Detecta mudanças entre df_original e editado, persiste, registra histórico."""
    mudancas = 0
    novas_hist = []
    df_novo = df_original.copy()

    for _, row in editado.iterrows():
        mask = (df_novo["Conjunto"] == row["Conjunto"]) & (df_novo["Item"] == row["Item"])
        if not mask.any():
            continue
        idx = df_novo[mask].index[0]
        for campo in ("Meta", "Real", "Pago"):
            novo = int(row[campo])
            antigo = int(df_novo.at[idx, campo])
            if novo != antigo:
                df_novo.at[idx, campo] = novo
                mudancas += 1
                novas_hist.append({
                    "Data": agora(),
                    "Conjunto": row["Conjunto"], "Item": row["Item"],
                    "Tipo": campo.upper(), "Valor": novo - antigo,
                    "Usuario": usuario_atual(),
                })
        df_novo.at[idx, "Status"] = calcular_status(
            int(df_novo.at[idx, "Meta"]), int(df_novo.at[idx, "Real"])
        )

    if mudancas == 0:
        st.info("Nenhuma alteração para salvar.")
        return

    salvar_estoque(df_novo)
    registrar_no_historico(novas_hist)
    st.success(f"{mudancas} alteração(ões) salvas.")
    st.rerun()


def acoes_item(df: pd.DataFrame, rotulo_sel: str):
    """Renderiza os 3 blocos de ação para um item escolhido."""
    conjunto, item_nome = parse_rotulo(rotulo_sel)
    if conjunto is None:
        return
    mask = (df["Conjunto"] == conjunto) & (df["Item"] == item_nome)
    if not mask.any():
        st.warning("Item não encontrado.")
        return
    linha = df[mask].iloc[0]
    idx = df[mask].index[0]

    st.caption(
        f"**{item_nome}** · {conjunto} · Meta: **{linha['Meta']}** · "
        f"Real: **{linha['Real']}** · Pago: **{linha['Pago']}** · Status: **{linha['Status']}**"
    )

    col1, col2, col3 = st.columns(3)

    # ----- Zerar Pago -----
    with col1:
        st.markdown("**Zerar Pago**")
        if st.button("Zerar pago deste item", key="btn_zerar_pago"):
            df.at[idx, "Pago"] = 0
            salvar_estoque(df)
            registrar_no_historico([{
                "Data": agora(),
                "Conjunto": conjunto, "Item": item_nome,
                "Tipo": "ZERAR_PAGO", "Valor": 0,
                "Usuario": usuario_atual(),
            }])
            st.rerun()

    # ----- Somar produção (incremental) -----
    with col2:
        st.markdown("**Somar produção**")
        with st.form("form_somar"):
            campo_add = st.selectbox("Coluna", ["Real", "Pago"])
            valor_add = st.number_input("Quantidade", min_value=1, value=1, step=1)
            submit = st.form_submit_button("Lançar")
        if submit:
            df.at[idx, campo_add] = int(df.at[idx, campo_add]) + int(valor_add)
            if campo_add == "Pago":
                # Pago também incrementa Real (mesma lógica do tkinter original)
                df.at[idx, "Real"] = int(df.at[idx, "Real"]) + int(valor_add)
            df.at[idx, "Status"] = calcular_status(
                int(df.at[idx, "Meta"]), int(df.at[idx, "Real"])
            )
            salvar_estoque(df)
            registrar_no_historico([{
                "Data": agora(),
                "Conjunto": conjunto, "Item": item_nome,
                "Tipo": campo_add.upper(), "Valor": int(valor_add),
                "Usuario": usuario_atual(),
            }])
            st.success(f"+{valor_add} em {campo_add}.")
            st.rerun()

    # ----- Excluir item -----
    with col3:
        st.markdown("**Excluir item**")
        confirmar = st.checkbox("Confirmo excluir", key="cb_excluir")
        if st.button("Excluir", disabled=not confirmar, key="btn_excluir"):
            df_novo = df[~mask]
            salvar_estoque(df_novo)
            registrar_no_historico([{
                "Data": agora(),
                "Conjunto": conjunto, "Item": item_nome,
                "Tipo": "REMOVE_ITEM", "Valor": "-",
                "Usuario": usuario_atual(),
            }])
            st.rerun()


def adicionar_item_form(df: pd.DataFrame):
    with st.form("form_add"):
        conjuntos_existentes = sorted(df["Conjunto"].unique().tolist())
        opcoes = conjuntos_existentes + ["+ Criar novo conjunto"]
        c1, c2, c3 = st.columns([2, 2, 1])
        conjunto_escolhido = c1.selectbox("Conjunto", opcoes)
        if conjunto_escolhido == "+ Criar novo conjunto":
            conjunto_final = c1.text_input("Nome do novo conjunto").strip()
        else:
            conjunto_final = conjunto_escolhido
        novo_item = c2.text_input("Nome do item").strip()
        meta_inicial = c3.number_input("Meta inicial", min_value=0, value=0, step=1)
        adicionar = st.form_submit_button("Adicionar", type="primary")

    if not adicionar:
        return
    if not conjunto_final or not novo_item:
        st.error("Preencha conjunto e item.")
        return
    if ((df["Conjunto"] == conjunto_final) & (df["Item"] == novo_item)).any():
        st.error(f"Já existe '{novo_item}' em '{conjunto_final}'.")
        return

    nova = pd.DataFrame([{
        "Conjunto": conjunto_final, "Item": novo_item,
        "Meta": int(meta_inicial), "Real": 0, "Pago": 0,
        "Status": calcular_status(int(meta_inicial), 0),
    }])
    df_novo = pd.concat([df, nova], ignore_index=True)
    salvar_estoque(df_novo)
    registrar_no_historico([{
        "Data": agora(),
        "Conjunto": conjunto_final, "Item": novo_item,
        "Tipo": "ADD_ITEM", "Valor": int(meta_inicial),
        "Usuario": usuario_atual(),
    }])
    st.success(f"'{novo_item}' adicionado em '{conjunto_final}'.")
    st.rerun()


def mostrar_historico():
    hist = carregar_historico()
    if hist.empty:
        st.write("Sem movimentações registradas.")
    else:
        st.dataframe(hist.iloc[::-1], use_container_width=True, hide_index=True, height=300)


def fechar_semana_form(df: pd.DataFrame):
    st.write(
        "Baixe a planilha da semana, depois zere os valores para começar a próxima "
        "(Meta, Real e Pago voltam a zero; histórico é limpo)."
    )
    with st.expander("Pré-visualizar resumo"):
        resumo = df.groupby("Conjunto").agg(
            Itens=("Item", "count"),
            Meta=("Meta", "sum"),
            Real=("Real", "sum"),
            Pago=("Pago", "sum"),
        ).reset_index()
        resumo["Diferenca"] = resumo["Real"] - resumo["Meta"]
        st.dataframe(resumo, use_container_width=True, hide_index=True)

    ts = datetime.now().strftime("%Y-%m-%d_%Hh%M")
    c1, c2 = st.columns(2)
    c1.download_button(
        "Baixar planilha da semana (.xlsx)",
        data=gerar_xlsx_fechamento(),
        file_name=f"fechamento_semana_{ts}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    confirmar = c2.checkbox("Já baixei, quero zerar")
    if st.button("Zerar planilha e iniciar nova semana", disabled=not confirmar):
        fechar_semana_reset()
        st.success("Planilha zerada.")
        st.rerun()


# ==================================================
# TELA ADMIN DE PAGAMENTO
# ==================================================

def tela_admin_pagamento():
    df = carregar_estoque()
    df["Faltando"] = df["Meta"] - df["Real"]
    em_debito = df[df["Faltando"] > 0].copy()

    header(df, subtitulo="Pagamento de itens em débito")

    if em_debito.empty:
        st.success("Nada em débito. Todos os itens estão dentro da meta.")
        return

    st.caption(
        f"**{len(em_debito)}** itens em débito · "
        f"**{int(em_debito['Faltando'].sum())}** peças faltando no total"
    )

    col_esq, col_dir = st.columns([2, 1])

    with col_esq:
        st.markdown("##### Itens em débito — clique numa linha para selecionar")
        df_show = em_debito[["Conjunto", "Item", "Meta", "Real", "Faltando"]].sort_values(
            ["Conjunto", "Faltando"], ascending=[True, False]
        ).reset_index(drop=True)

        evento = st.dataframe(
            df_show,
            use_container_width=True, hide_index=True,
            on_select="rerun", selection_mode="single-row",
            key="tab_debito",
            height=500,
        )
        linhas = evento.selection.rows if evento and evento.selection else []
        item_sel = None
        if linhas:
            row = df_show.iloc[linhas[0]]
            item_sel = (row["Conjunto"], row["Item"])

    with col_dir:
        if item_sel is None:
            st.markdown("##### Lançar pagamento")
            st.info("Selecione um item na tabela ao lado.")
            return

        conjunto, item_nome = item_sel
        linha = em_debito[
            (em_debito["Conjunto"] == conjunto) & (em_debito["Item"] == item_nome)
        ].iloc[0]
        faltando = int(linha["Faltando"])

        st.markdown(f"##### {item_nome}")
        st.caption(f"{conjunto}  ·  Faltam: **{faltando}**")

        with st.form("form_pag"):
            valor = st.number_input(
                "Quantidade paga agora",
                min_value=1, max_value=99999, value=min(faltando, 1) or 1, step=1,
            )
            enviar = st.form_submit_button("Lançar pagamento", type="primary")

        if not enviar:
            return

        idx = df[(df["Conjunto"] == conjunto) & (df["Item"] == item_nome)].index[0]
        df.at[idx, "Pago"] = int(df.at[idx, "Pago"]) + int(valor)
        df.at[idx, "Real"] = int(df.at[idx, "Real"]) + int(valor)
        df.at[idx, "Status"] = calcular_status(
            int(df.at[idx, "Meta"]), int(df.at[idx, "Real"])
        )
        df_save = df.drop(columns=["Faltando"], errors="ignore")
        salvar_estoque(df_save)
        registrar_no_historico([{
            "Data": agora(),
            "Conjunto": conjunto, "Item": item_nome,
            "Tipo": "PAGO", "Valor": int(valor),
            "Usuario": usuario_atual(),
        }])
        st.success(f"Pagamento de {valor} unidade(s) lançado.")
        st.rerun()


# ==================================================
# TELA VISUALIZADOR (somente leitura)
# ==================================================

def tela_visualizador():
    df = carregar_estoque()
    header(df, subtitulo="Modo leitura — sem permissão de edição")

    df_calc = df.copy()
    df_calc["Faltando"] = df_calc["Meta"] - df_calc["Real"]
    em_debito = df_calc[df_calc["Faltando"] > 0]
    if not em_debito.empty:
        st.caption(
            f"⚠ **{len(em_debito)}** itens em débito · "
            f"**{int(em_debito['Faltando'].sum())}** peças faltando no total"
        )

    busca = st.text_input("Buscar item", "", placeholder="Digite parte do nome...")
    df_show = aplicar_busca(df, busca).sort_values(["Conjunto", "Item"]).reset_index(drop=True)

    st.markdown("##### Estoque")
    st.dataframe(df_show, use_container_width=True, hide_index=True, height=500)

    with st.expander("Itens em débito (apenas faltantes)"):
        if em_debito.empty:
            st.success("Nada em débito.")
        else:
            st.dataframe(
                em_debito[["Conjunto", "Item", "Meta", "Real", "Faltando"]]
                .sort_values(["Conjunto", "Faltando"], ascending=[True, False]),
                use_container_width=True, hide_index=True,
            )

    with st.expander("Histórico"):
        mostrar_historico()


# ==================================================
# MAIN
# ==================================================

def main():
    st.set_page_config(
        page_title="Controle de Produção",
        page_icon="🔧",
        layout="wide",
    )

    problemas = diagnostico_secrets()
    if problemas:
        st.error("Configuração incompleta nos Secrets do Streamlit Cloud:")
        for p in problemas:
            st.markdown(f"- {p}")
        return

    if "logado" not in st.session_state:
        st.session_state["logado"] = False

    if not st.session_state["logado"]:
        tela_login()
        return

    perfil = st.session_state["perfil"]
    if perfil == "admin_completo":
        tela_admin_completo()
    elif perfil == "admin_pagamento":
        tela_admin_pagamento()
    elif perfil == "visualizador":
        tela_visualizador()
    else:
        st.error(f"Perfil desconhecido: {perfil}")


if __name__ == "__main__":
    main()
