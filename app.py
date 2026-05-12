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
from streamlit_sortables import sort_items


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
    "producao": {
        "senha_hash": "a074805448d1baef3240d5b2856df7345380cb875e9b1c46b27b3f4ed8a7ca32",
        "perfil": "produtor",
        "nome": "Produção",
    },
    "wagnerbender": {
        "senha_hash": "7e5e2e43c4eb7ab30d26677b8b4132f4ca74d154439489db5a46b477681387f3",
        "perfil": "visualizador",
        "nome": "Wagner Bender",
    },
}

DADOS_PADRAO = {
    "Conjunto Tampa Guia": [
        "TMG-001",
        "GXT-001",
        "TMÓ-001",
        "NLG-020",
        "RNG-001",
    ],
    "Conjunto Tampa Gás": [
        "TMÁ-001",
        "VLT-004",
        "ART-004",
        "NCL-004",
        "PRC-004",
        "RNG-001",
        # "TMT-004",  # do PDF, pendente — descomentar se for daqui
    ],
    "Conjunto Embolo": [
        "MBL-001",
        "BCK-001",
        "VTN-001",
    ],
    "Conjunto Diversos": [
        "RLP-001",
        "NLL-001",
        "BTH-001",
        "RLC-001",
        "RLM-001",
        "RLM-002",
        "PRC-012",
        "VLV-001",
        "VLV-002",
        "VLV-003",
        "VLA-001",
        # "TFL-001",  # do PDF, pendente — descomentar se for daqui
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
# HISTÓRICO DE PAGAMENTOS POR ITEM
# ==================================================

def historico_pagamentos_item(conjunto: str, item_nome: str) -> pd.DataFrame:
    """Retorna apenas os pagamentos (Tipo=PAGO) feitos no item escolhido."""
    return _historico_item_por_tipo(conjunto, item_nome, "PAGO")


def historico_producao_item(conjunto: str, item_nome: str) -> pd.DataFrame:
    """Retorna apenas os lançamentos de produção (Tipo=REAL) do item escolhido."""
    return _historico_item_por_tipo(conjunto, item_nome, "REAL")


def _historico_item_por_tipo(conjunto: str, item_nome: str, tipo: str) -> pd.DataFrame:
    hist = carregar_historico()
    if hist.empty:
        return pd.DataFrame(columns=["Data", "Valor", "Usuario"])
    filtrado = hist[
        (hist["Conjunto"] == conjunto)
        & (hist["Item"] == item_nome)
        & (hist["Tipo"] == tipo)
    ].copy()
    if filtrado.empty:
        return pd.DataFrame(columns=["Data", "Valor", "Usuario"])
    return filtrado[["Data", "Valor", "Usuario"]].iloc[::-1].reset_index(drop=True)


# ==================================================
# DIÁLOGOS DE CONFIRMAÇÃO (popups)
# ==================================================

@st.dialog("Confirmar alterações")
def dialog_confirmar_edicao(df_original: pd.DataFrame, editado: pd.DataFrame):
    """Mostra resumo das alterações inline antes de salvar."""
    mudancas = []
    for _, row in editado.iterrows():
        mask = (df_original["Conjunto"] == row["Conjunto"]) & (df_original["Item"] == row["Item"])
        if not mask.any():
            continue
        orig = df_original[mask].iloc[0]
        for campo in ("Meta", "Real", "Pago"):
            antigo = int(orig[campo])
            novo = int(row[campo])
            if antigo != novo:
                mudancas.append({
                    "Item": row["Item"],
                    "Campo": campo,
                    "De": antigo,
                    "Para": novo,
                    "Diferença": novo - antigo,
                })

    if not mudancas:
        st.info("Nenhuma alteração detectada.")
        if st.button("Fechar", use_container_width=True):
            st.rerun()
        return

    st.write(f"Você está prestes a aplicar **{len(mudancas)}** alteração(ões):")
    st.dataframe(pd.DataFrame(mudancas), use_container_width=True, hide_index=True)

    c1, c2 = st.columns(2)
    if c1.button("Confirmar e salvar", type="primary", use_container_width=True, key="dlg_edit_ok"):
        _aplicar_edicoes(df_original, editado, mudancas)
    if c2.button("Cancelar", use_container_width=True, key="dlg_edit_cancel"):
        st.rerun()


def _aplicar_edicoes(df_original: pd.DataFrame, editado: pd.DataFrame, mudancas: list):
    """Executa de fato as edições aprovadas no diálogo."""
    df_novo = df_original.copy()
    novas_hist = []
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
                novas_hist.append({
                    "Data": agora(),
                    "Conjunto": row["Conjunto"], "Item": row["Item"],
                    "Tipo": campo.upper(), "Valor": novo - antigo,
                    "Usuario": usuario_atual(),
                })
        df_novo.at[idx, "Status"] = calcular_status(
            int(df_novo.at[idx, "Meta"]), int(df_novo.at[idx, "Real"])
        )
    salvar_estoque(df_novo)
    registrar_no_historico(novas_hist)
    st.success(f"{len(mudancas)} alteração(ões) aplicadas.")
    st.rerun()


@st.dialog("Confirmar pagamento")
def dialog_confirmar_pagamento(conjunto: str, item_nome: str, valor: int, df: pd.DataFrame):
    """Mostra resumo antes de lançar o pagamento."""
    mask = (df["Conjunto"] == conjunto) & (df["Item"] == item_nome)
    if not mask.any():
        st.error("Item não encontrado.")
        return
    linha = df[mask].iloc[0]
    meta = int(linha["Meta"])
    real = int(linha["Real"])
    pago_atual = int(linha["Pago"])
    falta_antes = meta - real
    falta_depois = falta_antes - valor

    st.markdown(f"**Item:** {item_nome}")
    st.caption(f"{conjunto}")
    st.write("")

    c1, c2, c3 = st.columns(3)
    c1.metric("Pagamento agora", valor)
    c2.metric("Faltava", falta_antes)
    c3.metric("Faltará depois", max(falta_depois, 0))

    st.caption(
        f"Pago acumulado passará de **{pago_atual}** para **{pago_atual + valor}**.  \n"
        f"Real (produzido) passará de **{real}** para **{real + valor}**."
    )

    if falta_depois < 0:
        st.warning(
            f"Esse pagamento vai gerar excedente de {abs(falta_depois)} unidade(s) acima da meta."
        )

    c1, c2 = st.columns(2)
    if c1.button("Confirmar pagamento", type="primary", use_container_width=True, key="dlg_pag_ok"):
        _aplicar_pagamento(df, conjunto, item_nome, valor)
    if c2.button("Cancelar", use_container_width=True, key="dlg_pag_cancel"):
        st.rerun()


def _aplicar_pagamento(df: pd.DataFrame, conjunto: str, item_nome: str, valor: int):
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
# TELA ADMIN COMPLETO
# ==================================================

def tela_admin_completo():
    df = carregar_estoque()
    header(df)

    busca = st.text_input("Buscar item", "", placeholder="Digite parte do nome...")
    df_view = aplicar_busca(df, busca).reset_index(drop=True)

    # ============ Tabela editável (edição inline) ============
    st.markdown("##### Itens — duplo clique numa célula de Meta/Real/Pago para editar")

    editado = st.data_editor(
        df_view,
        use_container_width=True, hide_index=True,
        disabled=["Conjunto", "Item", "Status", "Pago"],
        column_config={
            "Conjunto": st.column_config.TextColumn(width="medium"),
            "Item": st.column_config.TextColumn(width="medium"),
            "Meta": st.column_config.NumberColumn(width="small", min_value=0, step=1),
            "Real": st.column_config.NumberColumn(width="small", min_value=0, step=1),
            "Pago": st.column_config.NumberColumn(width="small", help="Apenas o perfil Pagamento pode alterar"),
            "Status": st.column_config.TextColumn(width="small"),
        },
        num_rows="fixed",
        height=500,
        key="editor_estoque",
    )

    c1, c2 = st.columns([1, 4])
    if c1.button("Salvar alterações", type="primary", key="btn_salvar_edicoes"):
        dialog_confirmar_edicao(df, editado)

    st.divider()

    # ============ Ações em item específico ============
    st.markdown("##### Ações em um item específico")
    st.caption("Para zerar pago, somar produção (incremental) ou excluir item.")

    rotulos = [rotulo_item(r["Conjunto"], r["Item"]) for _, r in df.iterrows()]
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

    with st.expander("Reordenar itens (arraste para mudar a ordem)"):
        reordenar_itens_form(df)

    with st.expander("Histórico"):
        mostrar_historico()

    with st.expander("Fechar Semana"):
        fechar_semana_form(df)

    with st.expander("Resetar estoque para a estrutura padrão"):
        st.warning(
            "Apaga **tudo** que está na planilha do Google Sheets (estoque + histórico) "
            "e recria do zero usando a lista de códigos padrão definida no `DADOS_PADRAO` do app. "
            "Use quando os nomes dos itens estiverem desatualizados ou quando quiser limpar completamente."
        )
        confirmar_reset = st.checkbox("Sim, eu sei que vou perder todos os dados atuais")
        if st.button("Resetar agora", disabled=not confirmar_reset, key="btn_reset"):
            inicializar_estoque()
            st.success("Estoque resetado para a estrutura padrão.")
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

    col1, col2 = st.columns(2)

    # ----- Somar produção (incremental, só Real) -----
    with col1:
        st.markdown("**Somar produção (Real)**")
        st.caption("Adiciona unidades produzidas ao Real do item.")
        with st.form("form_somar"):
            valor_add = st.number_input("Quantidade", min_value=1, value=1, step=1)
            submit = st.form_submit_button("Lançar")
        if submit:
            idx = df[mask].index[0]
            df.at[idx, "Real"] = int(df.at[idx, "Real"]) + int(valor_add)
            df.at[idx, "Status"] = calcular_status(
                int(df.at[idx, "Meta"]), int(df.at[idx, "Real"])
            )
            salvar_estoque(df)
            registrar_no_historico([{
                "Data": agora(),
                "Conjunto": conjunto, "Item": item_nome,
                "Tipo": "REAL", "Valor": int(valor_add),
                "Usuario": usuario_atual(),
            }])
            st.success(f"+{valor_add} em Real.")
            st.rerun()

    # ----- Excluir item -----
    with col2:
        st.markdown("**Excluir item**")
        st.caption("Remove o item da planilha permanentemente.")
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


def reordenar_itens_form(df: pd.DataFrame):
    """Permite reordenar e mover itens entre conjuntos via drag-and-drop."""
    st.caption(
        "Arraste os itens para reordenar dentro de um conjunto **ou mover entre conjuntos**. "
        "Depois clique em **Salvar nova ordem**."
    )

    estrutura = [
        {
            "header": conjunto,
            "items": df[df["Conjunto"] == conjunto]["Item"].tolist(),
        }
        for conjunto in df["Conjunto"].unique()
    ]

    nova_estrutura = sort_items(estrutura, multi_containers=True, key="sort_all")

    if st.button("Salvar nova ordem", type="primary", key="btn_salvar_ordem"):
        _aplicar_reordenacao(df, nova_estrutura)


def _aplicar_reordenacao(df_original: pd.DataFrame, nova_estrutura: list):
    """Salva a nova distribuição/ordem, preservando Meta/Real/Pago/Status de cada linha."""
    from collections import Counter

    # 1) Detecta duplicações no destino (mesmo item duas vezes no mesmo conjunto)
    pares_destino = [
        (g["header"], item) for g in nova_estrutura for item in g["items"]
    ]
    duplicados = [p for p, c in Counter(pares_destino).items() if c > 1]
    if duplicados:
        st.error(
            "Não foi possível salvar: existem itens repetidos no mesmo conjunto:\n\n"
            + "\n".join(f"- {c}  →  {i}" for c, i in duplicados)
        )
        return

    # 2) Pool de origens (lista de pares (Conjunto, Item) com seus valores)
    pool = []
    for _, row in df_original.iterrows():
        pool.append({
            "Conjunto": row["Conjunto"],
            "Item": row["Item"],
            "Meta": int(row["Meta"]),
            "Real": int(row["Real"]),
            "Pago": int(row["Pago"]),
            "Status": row["Status"],
        })

    # 3) Para cada item no destino, consome o melhor candidato do pool
    #    (prioriza item que veio do mesmo conjunto; se não, qualquer um com mesmo nome)
    linhas_novas = []
    for grupo in nova_estrutura:
        conjunto_destino = grupo["header"]
        for item in grupo["items"]:
            # Prioridade 1: mesmo conjunto e mesmo nome
            origem = next(
                (p for p in pool if p["Conjunto"] == conjunto_destino and p["Item"] == item),
                None,
            )
            # Prioridade 2: qualquer pool com mesmo nome (item foi movido)
            if origem is None:
                origem = next((p for p in pool if p["Item"] == item), None)
            if origem is None:
                st.error(f"Item '{item}' não foi encontrado no estoque original.")
                return
            pool.remove(origem)
            linhas_novas.append({
                "Conjunto": conjunto_destino,
                "Item": item,
                "Meta": origem["Meta"],
                "Real": origem["Real"],
                "Pago": origem["Pago"],
                "Status": origem["Status"],
            })

    df_novo = pd.DataFrame(linhas_novas)
    salvar_estoque(df_novo)
    st.success("Nova ordem salva.")
    st.rerun()


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
        linha_estoque = df[
            (df["Conjunto"] == conjunto) & (df["Item"] == item_nome)
        ].iloc[0]
        meta = int(linha_estoque["Meta"])
        real = int(linha_estoque["Real"])
        pago_acumulado = int(linha_estoque["Pago"])
        faltando = meta - real

        st.markdown(f"##### {item_nome}")
        st.caption(f"{conjunto}")

        c1, c2, c3 = st.columns(3)
        c1.metric("Meta", meta)
        c2.metric("Já pago", pago_acumulado)
        c3.metric("Falta pagar", max(faltando, 0))

        # Histórico de pagamentos deste item
        pagos_item = historico_pagamentos_item(conjunto, item_nome)
        if not pagos_item.empty:
            st.caption(f"**Pagamentos anteriores ({len(pagos_item)}):**")
            st.dataframe(pagos_item, use_container_width=True, hide_index=True, height=180)
        else:
            st.caption("Sem pagamentos anteriores registrados.")

        st.write("")
        with st.form("form_pag"):
            valor = st.number_input(
                "Quantidade a pagar agora",
                min_value=1, max_value=99999, value=min(faltando, 1) or 1, step=1,
            )
            enviar = st.form_submit_button("Lançar pagamento", type="primary")

        if enviar:
            dialog_confirmar_pagamento(conjunto, item_nome, int(valor), df)


@st.dialog("Confirmar lançamento de produção")
def dialog_confirmar_producao(conjunto: str, item_nome: str, valor: int, df: pd.DataFrame):
    """Mostra resumo antes de lançar produção (somar no Real)."""
    mask = (df["Conjunto"] == conjunto) & (df["Item"] == item_nome)
    if not mask.any():
        st.error("Item não encontrado.")
        return
    linha = df[mask].iloc[0]
    meta = int(linha["Meta"])
    real = int(linha["Real"])
    falta_antes = meta - real
    falta_depois = falta_antes - valor

    st.markdown(f"**Item:** {item_nome}")
    st.caption(f"{conjunto}")
    st.write("")

    c1, c2, c3 = st.columns(3)
    c1.metric("Produção agora", valor)
    c2.metric("Faltava", max(falta_antes, 0))
    c3.metric("Faltará depois", max(falta_depois, 0))

    st.caption(
        f"Real (produzido) passará de **{real}** para **{real + valor}**.  \n"
        f"Meta continua **{meta}**."
    )

    if falta_depois < 0:
        st.warning(
            f"Esse lançamento vai gerar excedente de {abs(falta_depois)} unidade(s) acima da meta."
        )

    c1, c2 = st.columns(2)
    if c1.button("Confirmar lançamento", type="primary", use_container_width=True, key="dlg_prod_ok"):
        _aplicar_producao(df, conjunto, item_nome, valor)
    if c2.button("Cancelar", use_container_width=True, key="dlg_prod_cancel"):
        st.rerun()


def _aplicar_producao(df: pd.DataFrame, conjunto: str, item_nome: str, valor: int):
    idx = df[(df["Conjunto"] == conjunto) & (df["Item"] == item_nome)].index[0]
    df.at[idx, "Real"] = int(df.at[idx, "Real"]) + int(valor)
    df.at[idx, "Status"] = calcular_status(
        int(df.at[idx, "Meta"]), int(df.at[idx, "Real"])
    )
    df_save = df.drop(columns=["Faltando"], errors="ignore")
    salvar_estoque(df_save)
    registrar_no_historico([{
        "Data": agora(),
        "Conjunto": conjunto, "Item": item_nome,
        "Tipo": "REAL", "Valor": int(valor),
        "Usuario": usuario_atual(),
    }])
    st.success(f"Lançamento de {valor} unidade(s) registrado.")
    st.rerun()


# ==================================================
# TELA PRODUTOR (só altera Real)
# ==================================================

def tela_produtor():
    df = carregar_estoque()
    df["Faltando"] = df["Meta"] - df["Real"]

    header(df, subtitulo="Lançamento de produção — apenas Real")

    busca = st.text_input("Buscar item", "", placeholder="Digite parte do nome...")
    df_view = aplicar_busca(df, busca).reset_index(drop=True)

    col_esq, col_dir = st.columns([2, 1])

    with col_esq:
        st.markdown("##### Itens — clique numa linha para selecionar")
        df_show = df_view[["Conjunto", "Item", "Meta", "Real", "Faltando", "Status"]].reset_index(drop=True)

        evento = st.dataframe(
            df_show,
            use_container_width=True, hide_index=True,
            on_select="rerun", selection_mode="single-row",
            key="tab_producao",
            height=500,
        )
        linhas = evento.selection.rows if evento and evento.selection else []
        item_sel = None
        if linhas:
            row = df_show.iloc[linhas[0]]
            item_sel = (row["Conjunto"], row["Item"])

    with col_dir:
        if item_sel is None:
            st.markdown("##### Lançar produção")
            st.info("Selecione um item na tabela ao lado.")
            return

        conjunto, item_nome = item_sel
        linha_estoque = df[
            (df["Conjunto"] == conjunto) & (df["Item"] == item_nome)
        ].iloc[0]
        meta = int(linha_estoque["Meta"])
        real = int(linha_estoque["Real"])
        faltando = meta - real

        st.markdown(f"##### {item_nome}")
        st.caption(f"{conjunto}")

        c1, c2, c3 = st.columns(3)
        c1.metric("Meta", meta)
        c2.metric("Produzido", real)
        c3.metric("Falta", max(faltando, 0))

        # Histórico de lançamentos deste item
        prods_item = historico_producao_item(conjunto, item_nome)
        if not prods_item.empty:
            st.caption(f"**Lançamentos anteriores ({len(prods_item)}):**")
            st.dataframe(prods_item, use_container_width=True, hide_index=True, height=180)
        else:
            st.caption("Sem lançamentos anteriores registrados.")

        st.write("")
        with st.form("form_prod"):
            valor = st.number_input(
                "Quantidade produzida agora",
                min_value=1, max_value=99999, value=min(max(faltando, 1), 999), step=1,
            )
            enviar = st.form_submit_button("Lançar produção", type="primary")

        if enviar:
            dialog_confirmar_producao(conjunto, item_nome, int(valor), df)


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
    elif perfil == "produtor":
        tela_produtor()
    elif perfil == "visualizador":
        tela_visualizador()
    else:
        st.error(f"Perfil desconhecido: {perfil}")


if __name__ == "__main__":
    main()
