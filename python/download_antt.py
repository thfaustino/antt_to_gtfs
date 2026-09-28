import io
import re
import pandas as pd
import requests

MESES_MAP = {
    "jan": 1,
    "fev": 2,
    "mar": 3,
    "abr": 4,
    "mai": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "set": 9,
    "out": 10,
    "nov": 11,
    "dez": 12,
}


def parse_competencia(resource_name: str) -> tuple[int, int] | None:
    """Extrai (ano, mes) a partir do nome do recurso."""
    pattern = r"(?i)\b(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)[a-z]*[\s\-_/]*(\d{2,4})\b"
    match = re.search(pattern, resource_name)
    if not match:
        return None

    mes_txt = match.group(1).lower()[:3]
    mes_num = MESES_MAP.get(mes_txt)
    ano_txt = match.group(2)
    ano_num = int(ano_txt) if len(ano_txt) == 4 else int(f"20{ano_txt}")

    return (ano_num, mes_num)


def carregar_dados_ultimo_mes(
    tipo_dado: str = "Empresas, Linhas e Seções",
    base_url: str = "https://dados.antt.gov.br",
    package_id: str = "gerenciamento-de-autorizacoes",
    sep: str = ";",
    encoding: str = "latin1",
    **kwargs,
) -> pd.DataFrame:
    """Consulta o CKAN da ANTT, localiza o recurso do último mês e carrega

    o CSV diretamente em um DataFrame do Pandas em memória.

    :param tipo_dado: Texto que identifica a tabela (ex: 'Empresas, Linhas e
    Seções',
                      'Horários', 'Pontos de Esquema').
    :param base_url: URL do portal CKAN.
    :param package_id: Slug do conjunto de dados.
    :param sep: Separador do CSV (padrão ';' para bases ANTT).
    :param encoding: Codificação dos caracteres (padrão 'latin1').
    :param kwargs: Argumentos adicionais repassados para pd.read_csv (ex:
    dtype, usecols).
    :return: pd.DataFrame com os dados da competência mais recente.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    # 1. Consulta metadados na API do CKAN
    endpoint = f"{base_url.rstrip('/')}/api/3/action/package_show"
    resp = requests.get(
        endpoint, params={"id": package_id}, headers=headers, timeout=30
    )
    resp.raise_for_status()

    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"Erro na API CKAN: {payload.get('error')}")

    resources = payload["result"].get("resources", [])

    # 2. Mapeia competências válidas dos recursos CSV
    recursos_validos = []
    for res in resources:
        name = res.get("name") or res.get("id")
        url = res.get("url")
        fmt = (res.get("format") or "").lower()

        # Garante que é um link válido e formato de texto/csv
        if not url or fmt in ["html", "pdf"]:
            continue

        comp = parse_competencia(name)
        if comp:
            recursos_validos.append({"resource": res, "competencia": comp})

    if not recursos_validos:
        raise ValueError(
            "Nenhuma competência mensal identificada nos recursos do dataset."
        )

    # 3. Determina a competência mais recente
    ultima_comp = max(r["competencia"] for r in recursos_validos)
    ano_max, mes_max = ultima_comp

    mes_str = [k for k, v in MESES_MAP.items() if v == mes_max][0].capitalize()
    print(f"Competência mais recente identificada: {mes_str}/{ano_max}")

    # 4. Localiza o recurso específico pedido (ex: Linhas e Seções)
    alvos = [
        r["resource"]
        for r in recursos_validos
        if r["competencia"] == ultima_comp
        and tipo_dado.lower() in (r["resource"].get("name") or "").lower()
    ]

    if not alvos:
        raise FileNotFoundError(
            f"Recurso com o termo '{tipo_dado}' não encontrado para {mes_str}/{ano_max}."
        )

    recurso_escolhido = alvos[0]
    download_url = recurso_escolhido["url"]
    print(f"Carregando recurso: {recurso_escolhido.get('name')}")
    print(f"URL: {download_url}")

    # 5. Baixa o conteúdo diretamente para a memória
    response = requests.get(download_url, headers=headers, timeout=120)
    response.raise_for_status()

    # 6. Carrega no Pandas via buffer BytesIO
    df = pd.read_csv(
        io.BytesIO(response.content), sep=sep, encoding=encoding, **kwargs
    )

    # Adiciona metadados de controle se for útil no pipeline
    df.attrs["competencia"] = f"{ano_max}-{mes_max:02d}"
    df.attrs["resource_name"] = recurso_escolhido.get("name")

    return df


# --- Exemplo de Execução ---
if __name__ == "__main__":
    # Carrega Empresas, Linhas e Seções do último mês
    df_linhas = carregar_dados_ultimo_mes(
        tipo_dado="Empresas, Linhas e Seções",
        sep=";",  # CSVs da ANTT costumam vir delimitados por ponto e vírgula
        encoding="latin1",  # e com codificação latin1/cp1252
        low_memory=False,
    )

    print("\nResumo do DataFrame:")
    print(f"Linhas x Colunas: {df_linhas.shape}")
    print(f"Competência: {df_linhas.attrs.get('competencia')}")
    print("\nPrimeiras linhas:")
    print(df_linhas.head(3))