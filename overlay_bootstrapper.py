import json

mapa_overlay = {} # Começa como um dicionário vazio
caminho_ficheiro = 'bootstrp_conf'

# 1. Abre o ficheiro
with open(caminho_ficheiro, 'r') as f:
    lista_de_nos = json.load(f) # Carrega a LISTA

# 2. Transforma a LISTA num DICIONÁRIO
for no in lista_de_nos:
    id_do_no = no['id']
    vizinhos_do_no = no['neighbors']
    mapa_overlay[id_do_no] = vizinhos_do_no

# Agora, 'mapa_overlay' é o nosso mapa rápido!
# print(mapa_overlay['R1']) # Isto devolveria ["10.0.0.2", "10.0.8.2", ...]