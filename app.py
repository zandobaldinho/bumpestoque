"""
Controle de Produção / Estoque — versão web online
Stack: Streamlit + Google Sheets (via gspread + google-auth)
Deploy: Streamlit Community Cloud (gratuito, gera link público)
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

# Usuários e senhas. Para gerar um hash novo:
#   import hashlib; print(hashlib.sha256("senha".encode()).hexdigest())
USUARIOS = {
    "admin": {
        "senha_hash": hashlib.sha256("admin123".encode()).hexdigest(),
        "perfil": "admin_completo",
        "nome": "Administrador",
    },
    "pagamento": {
        "senha_hash": hashlib.sha256("pag123".encode()).hexdigest(),
        "perfil": "admin_pagamento",
        "nome": "Admin de Pagamento",
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


# ==================================================
# DIAGNÓSTICO DOS SECRETS
# ==================================================

def diagnostico_secrets():
    problemas = []
    if "connections" not in st.secrets or "gsheets" not in st.secrets.get("connections", {}):
        return [
            "A seção `[connections.gsheets]` está faltando nos Secrets.",
            "Vá em **Settings → Secrets** do app no Streamlit Cloud e cole o bloco completo.",
        ]
    cfg = st.secrets["connections"]["gsheets"]
    if "spreadsheet" not in cfg or not str(cfg.get("spreadsheet", "")).startswith("http"):
        problemas.append("Falta `spreadsheet = \"https://...\"` na seção `[connections.gsheets]`.")
    if cfg.get("type") != "service_account":
        problemas.append("Falta `type = \"service_account\"` na seção `[connections.gsheets]`.")
    obrigatorios = ["project_id", "private_key_id", "private_key", "client_email", "client_id"]
    for campo in obrigatorios:
        if not cfg.get(campo):
            problemas.append(f"Falta o campo `{campo}` nos Secrets.")
    return problemas


# ==================================================
# GOOGLE SHEETS — via gspread
# ==================================================

@st.cache_resource
def get_gspread_client():
    """Cria um cliente gspread autenticado via service account."""
    cfg = dict(st.secrets["connections"]["gsheets"])
    # Remove campos que não são da credencial
    creds_dict = {k: v for k, v in cfg.items() if k not in ("spreadsheet", "worksheet")}
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_spreadsheet():
    """Abre a planilha pela URL configurada nos secrets."""
    url = st.secrets["connections"]["gsheets"]["spreadsheet"]
    client = get_gspread_client()
    return client.open_by_url(url)


def get_or_create_worksheet(nome: str, colunas: list):
    """Retorna a worksheet pelo nome. Se não existir, cria com o cabeçalho."""
    sh = get_spreadsheet()
    try:
        ws = sh.worksheet(nome)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=nome, rows=100, cols=max(len(colunas), 10))
        ws.append_row(colunas)
    return ws


def ler_worksheet(nome: str, colunas: list) -> pd.DataFrame:
    """Lê uma worksheet inteira como DataFrame. Retorna vazio se não tiver dados."""
    ws = get_or_create_worksheet(nome, colunas)
    registros = ws.get_all_records()  # lista de dicts (usa a 1ª linha como header)
    if not registros:
        return pd.DataFrame(columns=colunas)
    df = pd.DataFrame(registros)
    # Garante todas as colunas
    for c in colunas:
        if c not in df.columns:
            df[c] = ""
    return df[colunas]


def escrever_worksheet(nome: str, df: pd.DataFrame, colunas: list):
    """Sobrescreve a worksheet com o conteúdo do DataFrame."""
    ws = get_or_create_worksheet(nome, colunas)
    ws.clear()
    valores = [colunas] + df[colunas].astype(str).values.tolist()
    ws.update(valores, value_input_option="USER_ENTERED")


# Helpers de alto nível ----------------------------

def carregar_estoque() -> pd.DataFrame:
    df = ler_worksheet(WORKSHEET_ESTOQUE, COLUNAS_ESTOQUE)
    if df.empty:
        # Inicializa com a estrutura padrão
        return inicializar_estoque()
    return garantir_tipos(df)


def carregar_historico() -> pd.DataFrame:
    return ler_worksheet(WORKSHEET_HISTORICO, COLUNAS_HISTORICO)


def salvar_estoque(df: pd.DataFrame):
    df = garantir_tipos(df)
    escrever_worksheet(WORKSHEET_ESTOQUE, df, COLUNAS_ESTOQUE)


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


def registrar_no_historico(linhas):
    hist = carregar_historico()
    novas = pd.DataFrame(linhas)
    hist = pd.concat([hist, novas], ignore_index=True)
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
    st.markdown("## 🔒 Controle de Produção")
    st.caption("Faça login para continuar")
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
                "logado": True,
                "usuario": usuario.strip(),
                "perfil": info["perfil"],
                "nome": info["nome"],
            })
            st.rerun()


# ==================================================
# TELA — ADMIN DE PAGAMENTO (perfil restrito)
# ==================================================

def tela_admin_pagamento():
    st.markdown("### 💰 Pagamento de itens em débito")
    st.caption(f"Logado como **{st.session_state['nome']}**")

    df = carregar_estoque()
    df["Faltando"] = df["Meta"] - df["Real"]
    em_debito = df[df["Faltando"] > 0].copy().sort_values(
        ["Conjunto", "Faltando"], ascending=[True, False]
    )

    if em_debito.empty:
        st.success("🎉 Nada em débito. Todos os itens estão dentro da meta.")
        return

    col1, col2 = st.columns(2)
    col1.metric("Itens em débito", len(em_debito))
    col2.metric("Total de peças faltando", int(em_debito["Faltando"].sum()))

    st.divider()
    st.markdown("#### Itens em débito")
    st.dataframe(
        em_debito[["Conjunto", "Item", "Meta", "Real", "Faltando"]],
        use_container_width=True, hide_index=True,
    )

    st.divider()
    st.markdown("#### Lançar pagamento")
    st.caption("Cada pagamento incrementa **Pago** e **Real** simultaneamente.")

    with st.form("form_pagamento"):
        conjuntos = sorted(em_debito["Conjunto"].unique().tolist())
        conjunto = st.selectbox("Conjunto", conjuntos)
        itens = em_debito[em_debito["Conjunto"] == conjunto]["Item"].tolist()
        item = st.selectbox("Item", itens)
        faltando_item = int(em_debito[
            (em_debito["Conjunto"] == conjunto) & (em_debito["Item"] == item)
        ]["Faltando"].iloc[0]) if itens else 0
        st.info(f"Faltam **{faltando_item}** peças para este item.")
        valor = st.number_input(
            "Quantidade paga agora",
            min_value=1, max_value=99999, value=min(faltando_item, 1) or 1, step=1,
        )
        enviar = st.form_submit_button("💰 Lançar pagamento", type="primary")

    if enviar:
        df_full = carregar_estoque()
        mask = (df_full["Conjunto"] == conjunto) & (df_full["Item"] == item)
        if not mask.any():
            st.error("Item não encontrado.")
            return
        idx = df_full[mask].index[0]
        df_full.at[idx, "Pago"] = int(df_full.at[idx, "Pago"]) + int(valor)
        df_full.at[idx, "Real"] = int(df_full.at[idx, "Real"]) + int(valor)
        df_full.at[idx, "Status"] = calcular_status(
            int(df_full.at[idx, "Meta"]), int(df_full.at[idx, "Real"])
        )
        salvar_estoque(df_full)
        registrar_no_historico([{
            "Data": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "Conjunto": conjunto, "Item": item,
            "Tipo": "PAGO", "Valor": int(valor),
            "Usuario": st.session_state["usuario"],
        }])
        st.success(f"Pagamento de {valor} unidade(s) lançado em '{item}'.")
        st.rerun()


# ==================================================
# TELA — ADMIN COMPLETO
# ==================================================

def tela_admin_completo():
    st.caption(f"Logado como **{st.session_state['nome']}** (acesso total)")

    aba1, aba2, aba3, aba4 = st.tabs([
        "📊 Estoque",
        "➕ Adicionar / Remover",
        "📜 Histórico",
        "🗓️ Fechar Semana",
    ])

    with aba1:
        df = carregar_estoque()
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Itens", len(df))
        col2.metric("Meta total", int(df["Meta"].sum()))
        col3.metric("Produzido", int(df["Real"].sum()))
        col4.metric("Diferença", int(df["Real"].sum() - df["Meta"].sum()))

        st.divider()
        busca = st.text_input("🔎 Buscar item", "")
        df_view = df.copy()
        if busca.strip():
            df_view = df_view[df_view["Item"].str.lower().str.contains(busca.lower())]

        st.markdown("##### Edite os valores e clique em **Salvar alterações**")
        editado = st.data_editor(
            df_view,
            use_container_width=True, hide_index=True,
            disabled=["Conjunto", "Item", "Status"],
            column_config={
                "Meta": st.column_config.NumberColumn(min_value=0, step=1),
                "Real": st.column_config.NumberColumn(min_value=0, step=1),
                "Pago": st.column_config.NumberColumn(min_value=0, step=1),
            },
            key="editor_estoque",
        )

        if st.button("💾 Salvar alterações", type="primary"):
            mudancas = 0
            novas_linhas_hist = []
            for _, row in editado.iterrows():
                mask = (df["Conjunto"] == row["Conjunto"]) & (df["Item"] == row["Item"])
                if not mask.any():
                    continue
                idx = df[mask].index[0]
                for campo in ("Meta", "Real", "Pago"):
                    novo = int(row[campo])
                    antigo = int(df.at[idx, campo])
                    if novo != antigo:
                        df.at[idx, campo] = novo
                        mudancas += 1
                        novas_linhas_hist.append({
                            "Data": datetime.now().strftime("%d/%m/%Y %H:%M"),
                            "Conjunto": row["Conjunto"], "Item": row["Item"],
                            "Tipo": campo.upper(), "Valor": novo - antigo,
                            "Usuario": st.session_state["usuario"],
                        })
                df.at[idx, "Status"] = calcular_status(
                    int(df.at[idx, "Meta"]), int(df.at[idx, "Real"])
                )
            salvar_estoque(df)
            if novas_linhas_hist:
                registrar_no_historico(novas_linhas_hist)
            st.success(f"{mudancas} alteração(ões) salvas.")
            st.rerun()

    with aba2:
        df = carregar_estoque()
        col_a, col_b = st.columns(2)

        with col_a:
            st.markdown("##### ➕ Adicionar item")
            with st.form("form_add"):
                conjuntos_existentes = sorted(df["Conjunto"].unique().tolist())
                opcoes = conjuntos_existentes + ["⊕ Criar novo conjunto"]
                conjunto_escolhido = st.selectbox("Conjunto", opcoes)
                if conjunto_escolhido == "⊕ Criar novo conjunto":
                    conjunto_final = st.text_input("Nome do novo conjunto").strip()
                else:
                    conjunto_final = conjunto_escolhido
                novo_item = st.text_input("Nome do item").strip()
                meta_inicial = st.number_input("Meta inicial", min_value=0, value=0, step=1)
                adicionar = st.form_submit_button("Adicionar", type="primary")

            if adicionar:
                if not conjunto_final or not novo_item:
                    st.error("Preencha conjunto e item.")
                elif ((df["Conjunto"] == conjunto_final) & (df["Item"] == novo_item)).any():
                    st.error(f"Já existe '{novo_item}' em '{conjunto_final}'.")
                else:
                    nova = pd.DataFrame([{
                        "Conjunto": conjunto_final, "Item": novo_item,
                        "Meta": int(meta_inicial), "Real": 0, "Pago": 0,
                        "Status": calcular_status(int(meta_inicial), 0),
                    }])
                    df = pd.concat([df, nova], ignore_index=True)
                    salvar_estoque(df)
                    registrar_no_historico([{
                        "Data": datetime.now().strftime("%d/%m/%Y %H:%M"),
                        "Conjunto": conjunto_final, "Item": novo_item,
                        "Tipo": "ADD_ITEM", "Valor": int(meta_inicial),
                        "Usuario": st.session_state["usuario"],
                    }])
                    st.success(f"'{novo_item}' adicionado em '{conjunto_final}'.")
                    st.rerun()

        with col_b:
            st.markdown("##### 🗑️ Remover item")
            with st.form("form_remove"):
                conjunto_rm = st.selectbox(
                    "Conjunto", sorted(df["Conjunto"].unique().tolist()), key="rm_conj",
                )
                itens_disponiveis = df[df["Conjunto"] == conjunto_rm]["Item"].tolist()
                item_rm = st.selectbox("Item", itens_disponiveis, key="rm_item")
                confirmar = st.checkbox("Confirmo a remoção")
                remover = st.form_submit_button("Remover", type="secondary")

            if remover:
                if not confirmar:
                    st.warning("Marque a confirmação antes de remover.")
                else:
                    df = df[~((df["Conjunto"] == conjunto_rm) & (df["Item"] == item_rm))]
                    salvar_estoque(df)
                    registrar_no_historico([{
                        "Data": datetime.now().strftime("%d/%m/%Y %H:%M"),
                        "Conjunto": conjunto_rm, "Item": item_rm,
                        "Tipo": "REMOVE_ITEM", "Valor": "-",
                        "Usuario": st.session_state["usuario"],
                    }])
                    st.success(f"'{item_rm}' removido de '{conjunto_rm}'.")
                    st.rerun()

    with aba3:
        hist = carregar_historico()
        if hist.empty:
            st.info("Sem movimentações registradas ainda.")
        else:
            st.dataframe(hist.iloc[::-1], use_container_width=True, hide_index=True)

    with aba4:
        st.markdown("##### 🗓️ Fechar a semana")
        st.warning(
            "Esta ação vai:\n\n"
            "1. **Gerar um arquivo `.xlsx`** com Estoque Final, Histórico e Resumo\n"
            "2. **Zerar** Meta, Real e Pago (mantendo conjuntos/itens)\n"
            "3. **Limpar** o histórico"
        )

        with st.expander("👁️ Pré-visualizar resumo"):
            df = carregar_estoque()
            resumo = df.groupby("Conjunto").agg(
                Itens=("Item", "count"),
                Meta=("Meta", "sum"),
                Real=("Real", "sum"),
                Pago=("Pago", "sum"),
            ).reset_index()
            resumo["Diferenca"] = resumo["Real"] - resumo["Meta"]
            st.dataframe(resumo, use_container_width=True, hide_index=True)

        st.markdown("**Passo 1 — Baixe o arquivo da semana:**")
        ts = datetime.now().strftime("%Y-%m-%d_%Hh%M")
        st.download_button(
            "⬇️ Baixar planilha da semana (.xlsx)",
            data=gerar_xlsx_fechamento(),
            file_name=f"fechamento_semana_{ts}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        st.markdown("**Passo 2 — Depois de baixar, zere a planilha:**")
        confirmar = st.checkbox("Já baixei e confirmo que quero zerar a planilha")
        if st.button("🔒 Zerar planilha e iniciar nova semana", disabled=not confirmar):
            fechar_semana_reset()
            st.success("Planilha zerada. Boa nova semana!")
            st.rerun()


# ==================================================
# MAIN
# ==================================================

def main():
    st.set_page_config(
        page_title="Controle de Produção — Bump",
        page_icon="🔧",
        layout="wide",
    )

    problemas = diagnostico_secrets()
    if problemas:
        st.error("⚠️ Configuração incompleta nos Secrets do Streamlit Cloud:")
        for p in problemas:
            st.markdown(f"- {p}")
        return

    if "logado" not in st.session_state:
        st.session_state["logado"] = False

    if not st.session_state["logado"]:
        tela_login()
        return

    st.markdown("# 🔧 Controle de Produção")
    with st.sidebar:
        st.markdown(f"**Usuário:** {st.session_state['nome']}")
        st.markdown(f"**Perfil:** `{st.session_state['perfil']}`")
        if st.button("🚪 Sair"):
            for k in ("logado", "usuario", "perfil", "nome"):
                st.session_state.pop(k, None)
            st.rerun()

    perfil = st.session_state["perfil"]
    if perfil == "admin_completo":
        tela_admin_completo()
    elif perfil == "admin_pagamento":
        tela_admin_pagamento()
    else:
        st.error(f"Perfil desconhecido: {perfil}")


if __name__ == "__main__":
    main()
