import io
import os
import re
import time
import pandas as pd
import requests
from dotenv import load_dotenv
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError

load_dotenv()

MESES_MAP = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}

UFS = {
    'AC': 'Acre', 'AL': 'Alagoas', 'AP': 'Amapá', 'AM': 'Amazonas',
    'BA': 'Bahia', 'CE': 'Ceará', 'DF': 'Distrito Federal', 'ES': 'Espírito Santo',
    'GO': 'Goiás', 'MA': 'Maranhão', 'MT': 'Mato Grosso', 'MS': 'Mato Grosso do Sul',
    'MG': 'Minas Gerais', 'PA': 'Pará', 'PB': 'Paraíba', 'PR': 'Paraná',
    'PE': 'Pernambuco', 'PI': 'Piauí', 'RJ': 'Rio de Janeiro', 'RN': 'Rio Grande do Norte',
    'RS': 'Rio Grande do Sul', 'RO': 'Rondônia', 'RR': 'Roraima', 'SC': 'Santa Catarina',
    'SP': 'São Paulo', 'SE': 'Sergipe', 'TO': 'Tocantins'
}

geolocator = Nominatim(user_agent="antt_to_gtfs_pipeline_br", timeout=10)


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
    tipo_dado: str = "Pontos do Esquema Operacional",
    base_url: str = "https://dados.antt.gov.br",
    package_id: str = "gerenciamento-de-autorizacoes",
    sep: str = ";",
    encoding: str = "latin1",
    **kwargs,
) -> pd.DataFrame:
    """Consulta o CKAN da ANTT, localiza o recurso do último mês e carrega o CSV em memória."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    endpoint = f"{base_url.rstrip('/')}/api/3/action/package_show"
    resp = requests.get(endpoint, params={"id": package_id}, headers=headers, timeout=30)
    resp.raise_for_status()

    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"Erro na API CKAN: {payload.get('error')}")

    resources = payload["result"].get("resources", [])

    recursos_validos = []
    for res in resources:
        name = res.get("name") or res.get("id")
        url = res.get("url")
        fmt = (res.get("format") or "").lower()

        if not url or fmt in ["html", "pdf"]:
            continue

        comp = parse_competencia(name)
        if comp:
            recursos_validos.append({"resource": res, "competencia": comp})

    if not recursos_validos:
        raise ValueError("Nenhuma competência mensal identificada nos recursos do dataset.")

    ultima_comp = max(r["competencia"] for r in recursos_validos)
    ano_max, mes_max = ultima_comp
    mes_str = [k for k, v in MESES_MAP.items() if v == mes_max][0].capitalize()
    print(f"Competência mais recente identificada: {mes_str}/{ano_max}")

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

    response = requests.get(download_url, headers=headers, timeout=120)
    response.raise_for_status()

    df = pd.read_csv(io.BytesIO(response.content), sep=sep, encoding=encoding, **kwargs)
    df.attrs["competencia"] = f"{ano_max}-{mes_max:02d}"
    df.attrs["resource_name"] = recurso_escolhido.get("name")

    return df


def extrair_cidade_estado(texto: str) -> str | None:
    """Extrai 'Cidade, Estado, Brasil' de formatos como '... , Alvorada - TO , Brasil'"""
    if not texto or pd.isna(texto):
        return None
    padrao = r',\s*([^,-]+)\s*-\s*([A-Za-z]{2})\s*,'
    match = re.search(padrao, str(texto))
    if match:
        cidade = match.group(1).strip()
        sigla_uf = match.group(2).upper().strip()
        nome_estado = UFS.get(sigla_uf, sigla_uf)
        return f"{cidade}, {nome_estado}, Brasil"
    return None


def buscar_com_fallback(texto_completo: str) -> tuple[float | None, float | None]:
    """Tenta o nome completo; se falhar, busca por 'Cidade, Estado, Brasil'."""
    if not texto_completo or pd.isna(texto_completo):
        return None, None

    texto_limpo = str(texto_completo).strip()

    # 1. Tentativa com nome original
    try:
        loc = geolocator.geocode(texto_limpo, country_codes="br")
        if loc:
            return loc.latitude, loc.longitude
    except (GeocoderTimedOut, GeocoderServiceError):
        time.sleep(2.0)
    except Exception:
        pass

    # 2. Fallback: 'Cidade, Estado, Brasil' (formato com 100% de precisão no OSM)
    query_cidade = extrair_cidade_estado(texto_limpo)
    if query_cidade:
        time.sleep(1.1)
        try:
            loc = geolocator.geocode(query_cidade, country_codes="br")
            if loc:
                return loc.latitude, loc.longitude
        except (GeocoderTimedOut, GeocoderServiceError):
            time.sleep(2.0)
        except Exception:
            pass

    return None, None


if __name__ == "__main__":
    pd.set_option("display.max_columns", None)

    # 1. Carrega dados de Pontos da ANTT
    print("Baixando base de pontos da ANTT...")
    df_pontos = carregar_dados_ultimo_mes(
        tipo_dado="Pontos do Esquema Operacional",
        sep=";",
        encoding="latin1",
        low_memory=False,
    )

    # 2. Preparação das colunas do GTFS stops
    df_pontos["nm_corrigido"] = df_pontos["nome_ponto_parada"].astype(str).str[7:].str.strip()
    df_pontos["stop_id"] = df_pontos["nome_ponto_parada"].astype(str).str[:7].str.strip()
    df_pontos["nm_geo"] = (
        df_pontos["nm_corrigido"] + " , " + df_pontos["municipio_uf"].astype(str) + " , Brasil"
    )

    # 3. Extrai pontos únicos para georreferenciamento
    arquivo_saida = "ped_geo_completo.csv"
    
    # Se o arquivo já existir com dados parciais, retoma dele
    if os.path.exists(arquivo_saida):
        print(f"Arquivo existente encontrado. Retomando de '{arquivo_saida}'...")
        ped_geo = pd.read_csv(arquivo_saida)
    else:
        ped_geo = df_pontos[["stop_id", "nm_geo", "nm_corrigido"]].copy()
        ped_geo.drop_duplicates(subset=["stop_id"], inplace=True)
        ped_geo["latitude"] = None
        ped_geo["longitude"] = None

    # 4. Filtra apenas os registros que ainda não possuem latitude
    pendentes = ped_geo[ped_geo["latitude"].isna() | ped_geo["longitude"].isna()]
    total_pendentes = len(pendentes)
    print(f"\nTotal de pontos sem coordenadas: {total_pendentes} de {len(ped_geo)}")

    # 5. Loop de busca com rate limiter
    for cont, idx in enumerate(pendentes.index, start=1):
        endereco = ped_geo.loc[idx, "nm_geo"]
        lat, lon = buscar_com_fallback(endereco)

        ped_geo.loc[idx, "latitude"] = lat
        ped_geo.loc[idx, "longitude"] = lon

        time.sleep(1.1)  # Respeita o rate limit de 1 req/s do OpenStreetMap

        if cont % 20 == 0 or cont == total_pendentes:
            ped_geo.to_csv(arquivo_saida, index=False, encoding="utf-8-sig")
            print(f"Progresso: {cont}/{total_pendentes} processados...")

    print(f"\nGeocodificação concluída! Base final salva em '{arquivo_saida}'.")
    print(ped_geo.head())