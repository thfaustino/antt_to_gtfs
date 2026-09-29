import os
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

# 1. Configurações de Conexão com o PostgreSQL
HOST = os.getenv("PG_HOST", "localhost")
PORT = os.getenv("PG_PORT", "5432")
DATABASE = os.getenv("PG_DATABASE", "intermunicipal")
USER = os.getenv("PG_USER", "postgres")
PASSWORD = os.getenv("PG_PASSWORD", "")

engine = create_engine(f"postgresql://{USER}:{PASSWORD}@{HOST}:{PORT}/{DATABASE}")


def associar_e_salvar_stops(
    df_antt: pd.DataFrame,
    schema_origem: str = "public",     # Schema onde está a tabela stops SEINFRA já georreferenciada
    tabela_origem: str = "stops",      # Nome da tabela existente
    schema_destino: str = "gtfs_antt", # Schema novo para os dados da ANTT
    tabela_destino: str = "stops",
    raio_metros: float = 1000.0        # Buffer de 1 km
):
    print("Iniciando processo de associação espacial...")

    # Garante estrutura inicial da tabela temporária da ANTT
    df_temp = df_antt.dropna(subset=["latitude", "longitude"]).copy()
    
    stops_base = pd.DataFrame()
    stops_base["stop_id"] = df_temp["stop_id"].astype(str).str.strip()
    stops_base["stop_name"] = df_temp["nm_corrigido"].astype(str).str.strip()
    stops_base["stop_lat"] = df_temp["latitude"].astype(float)
    stops_base["stop_lon"] = df_temp["longitude"].astype(float)
    stops_base["nm_geo"] = df_temp["nm_geo"].astype(str)
    stops_base["location_type"] = 0
    stops_base["wheelchair_boarding"] = 0
    
    # Identifica se o ponto pertence a Minas Gerais
    # Verifica tanto pelo nome quanto pela sigla MG
    stops_base["is_mg"] = stops_base["nm_geo"].str.contains(r"\bMG\b|Minas Gerais", case=False, regex=True)
    stops_base.drop_duplicates(subset=["stop_id"], inplace=True)

    with engine.begin() as conn:
        # Garante a existência da extensão PostGIS e dos schemas
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis;"))
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_destino};"))

        # Carrega a base provisória da ANTT em uma tabela temporária (ou de staging)
        stops_base.to_sql(
            name="_staging_antt_stops",
            con=conn,
            schema=schema_destino,
            if_exists="replace",
            index=False
        )

        print(f"Buscando vizinhos mais próximos no raio de {raio_metros/1000:.1f} km para pontos de MG...")

        # 2. Query Espacial:
        # - Utiliza ST_DWithin com 'geography' para cálculo em metros
        # - LATERAL JOIN com ST_Distance para capturar o ponto mais próximo dentro de 1 km
        # - CASE WHEN substitui as coordenadas apenas para os pontos associados
        query_associacao = text(f"""
            CREATE TABLE {schema_destino}.{tabela_destino} AS
            SELECT 
                a.stop_id,
                a.stop_name,
                -- Se encontrou na SEINFRA dentro de 1km em MG, assume as coordenadas da SEINFRA
                COALESCE(vizinho.stop_lat, a.stop_lat) AS stop_lat,
                COALESCE(vizinho.stop_lon, a.stop_lon) AS stop_lon,
                a.location_type,
                a.wheelchair_boarding,
                vizinho.stop_id AS stop_id_seinfra,
                vizinho.distancia_metros
            FROM {schema_destino}._staging_antt_stops a
            LEFT JOIN LATERAL (
                SELECT 
                    s.stop_id,
                    s.stop_lat,
                    s.stop_lon,
                    ST_Distance(
                        ST_SetSRID(ST_MakePoint(a.stop_lon, a.stop_lat), 4326)::geography,
                        ST_SetSRID(ST_MakePoint(s.stop_lon, s.stop_lat), 4326)::geography
                    ) AS distancia_metros
                FROM {schema_origem}.{tabela_origem} s
                WHERE a.is_mg = TRUE
                  AND ST_DWithin(
                      ST_SetSRID(ST_MakePoint(a.stop_lon, a.stop_lat), 4326)::geography,
                      ST_SetSRID(ST_MakePoint(s.stop_lon, s.stop_lat), 4326)::geography,
                      :raio
                  )
                ORDER BY distancia_metros ASC
                LIMIT 1
            ) vizinho ON TRUE;
        """)

        # Remove a tabela final se já existir e cria a nova com os dados associados
        conn.execute(text(f"DROP TABLE IF EXISTS {schema_destino}.{tabela_destino};"))
        conn.execute(query_associacao, {"raio": raio_metros})

        # 3. Adiciona Chave Primária, Coluna Geométrica PostGIS e Índices
        conn.execute(text(f"""
            ALTER TABLE {schema_destino}.{tabela_destino} ADD PRIMARY KEY (stop_id);
            
            ALTER TABLE {schema_destino}.{tabela_destino} 
            ADD COLUMN geom geometry(Point, 4326);
            
            UPDATE {schema_destino}.{tabela_destino} 
            SET geom = ST_SetSRID(ST_MakePoint(stop_lon, stop_lat), 4326);
            
            CREATE INDEX idx_{tabela_destino}_geom 
            ON {schema_destino}.{tabela_destino} USING GIST (geom);
            
            -- Limpeza da tabela intermediária
            DROP TABLE IF EXISTS {schema_destino}._staging_antt_stops;
        """))

        # Relatório de conferência
        res = conn.execute(text(f"""
            SELECT 
                COUNT(*) AS total,
                COUNT(stop_id_seinfra) AS associados_seinfra,
                ROUND(AVG(distancia_metros)::numeric, 1) AS dist_media_m
            FROM {schema_destino}.{tabela_destino};
        """)).fetchone()

        print("\n=== Resultado da Integração Espacial ===")
        print(f"Total de paradas ANTT gravadas: {res[0]}")
        print(f"Paradas associadas à base SEINFRA (raio <= 1km): {res[1]}")
        if res[1] > 0:
            print(f"Distância média da associação: {res[2]} metros")
        print(f"Tabela final criada em: '{schema_destino}.{tabela_destino}'")


if __name__ == "__main__":
    # Supondo que você já tem o DataFrame 'ped_geo' em memória após a geocodificação
    # ou pode carregar do CSV:
    ped_geo = pd.read_csv("ped_geo_completo.csv")
    
    associar_e_salvar_stops(
        df_antt=ped_geo,
        schema_origem="gtfs",       # Altere se a tabela stops anterior estiver em outro schema (ex: "gtfs_seinfra")
        tabela_origem="stops",
        schema_destino="gtfs_antt",
        tabela_destino="stops",
        raio_metros=1000.0            # 1 km
    )