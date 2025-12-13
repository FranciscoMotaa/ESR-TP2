# 🔒 Guia de Teste: Sistema de Criptografia AES-256-GCM

**Data**: 13 de Dezembro de 2025  
**Versão**: 1.0

---

## 📋 Pré-requisitos

### 1. Instalar Biblioteca de Criptografia
```bash
pip3 install cryptography
```

### 2. Verificar Instalação
```bash
python3 -c "from cryptography.hazmat.primitives.ciphers.aead import AESGCM; print('✓ cryptography instalada')"
```

### 3. Verificar AES-NI (Hardware Acceleration)
```bash
lscpu | grep -i aes
# Se aparecer "aes", tens aceleração hardware (100x mais rápido!)
```

---

## 🧪 Testes Básicos

### **Teste 1: Criptografia Ativada com Chave Padrão**

#### Objetivo
Verificar que o sistema inicia com criptografia ativa e consegue cifrar/decifrar pacotes.

#### Passos

**1. Iniciar Tracker**
```bash
python3 bootstrapper.py
```

**2. Iniciar Streamer (Terminal 2)**
```bash
python3 main.py STREAMER1 --tracker 10.0.10.20
```

**Saída Esperada**:
```
[*] Nó STREAMER1 (10.0.10.X) ONLINE
[SECURITY] Criptografia AES-256-GCM ativada ✓
[*] Modo seguro: Conteúdo de vídeo será cifrado AES-256-GCM
```

**3. Iniciar Cliente (Terminal 3)**
```bash
python3 main.py C6 --tracker 10.0.10.20
```

**Saída Esperada**:
```
[*] Nó C6 (10.0.10.Y) ONLINE
[SECURITY] Criptografia AES-256-GCM ativada ✓
[*] Modo seguro: Conteúdo de vídeo será cifrado AES-256-GCM
```

**4. Fazer JOIN (no cliente)**
```bash
join STREAMER1
```

**5. Verificar Logs**

No **Streamer**:
```
[STREAMER-STATS] Frames: 200 | FEC: 50 | Clientes: 1
[STREAMER-STATS] Cifrados: 200 pacotes 🔒
```

No **Cliente**:
```
[CLIENTE-INIT] Criptografia AES-256-GCM: ATIVA 🔒
[CLIENTE-STATS] Recebidos: 200 | Decifrados: 200 🔒
```

#### Resultado
✅ **PASSOU** se:
- Ambos mostram "Criptografia AES-256-GCM ativada"
- Streamer mostra "Cifrados: X pacotes"
- Cliente mostra "Decifrados: X pacotes"
- Stream funciona normalmente (vídeo aparece)

---

### **Teste 2: Chave Personalizada**

#### Objetivo
Testar com chave custom via variável ambiente.

#### Passos

**1. Definir Chave Custom**
```bash
export STREAM_KEY="minha_chave_secreta_2025"
```

**2. Iniciar Streamer**
```bash
python3 main.py STREAMER1 --tracker 10.0.10.20
```

**3. Iniciar Cliente (MESMA chave)**
```bash
export STREAM_KEY="minha_chave_secreta_2025"
python3 main.py C6 --tracker 10.0.10.20
```

**4. Fazer JOIN**
```bash
join STREAMER1
```

#### Resultado
✅ **PASSOU** se stream funciona normalmente.

---

### **Teste 3: Chaves Incompatíveis (DEVE FALHAR)**

#### Objetivo
Verificar que clientes com chaves diferentes não conseguem decifrar.

#### Passos

**1. Streamer com chave A**
```bash
export STREAM_KEY="chave_A"
python3 main.py STREAMER1 --tracker 10.0.10.20
```

**2. Cliente com chave B (DIFERENTE)**
```bash
export STREAM_KEY="chave_B"
python3 main.py C6 --tracker 10.0.10.20
```

**3. Fazer JOIN**
```bash
join STREAMER1
```

#### Resultado Esperado
❌ Cliente **NÃO consegue** reproduzir:
```
[SECURITY] Falha autenticação pacote seq=1 de 10.0.10.X
[SECURITY] Falha autenticação pacote seq=2 de 10.0.10.X
[CLIENTE-WARN] Falhas autenticação: 100
```

✅ **PASSOU** se cliente reporta falhas de autenticação.

---

### **Teste 4: Sniffing da Rede (Wireshark)**

#### Objetivo
Verificar que pacotes capturados na rede estão cifrados.

#### Passos

**1. Iniciar Captura Wireshark**
```bash
sudo wireshark
# Selecionar interface de rede (ex: eth0)
# Filtro: udp.port == 50000
```

**2. Iniciar Streamer + Cliente**
```bash
# Terminal 1
python3 main.py STREAMER1 --tracker 10.0.10.20

# Terminal 2
python3 main.py C6 --tracker 10.0.10.20
join STREAMER1
```

**3. Analisar Pacotes no Wireshark**

**Pacotes HELLO/ROUTE_DISCOVERY (Claro)**:
```
Header: Tipo=1, Src=10.0.10.X, ...
Payload: {"ack_seq": 123} ← LEGÍVEL
```

**Pacotes STREAM_DATA (Cifrado)**:
```
Header: Tipo=5, Src=10.0.10.X, Encrypted=1, ...
IV: 0x3f2a... (12 bytes aleatórios)
Payload: 0xf4b2e1... ← ILEGÍVEL (ruído aleatório)
Tag: 0x8c3d... (16 bytes)
```

#### Resultado
✅ **PASSOU** se:
- Payloads de STREAM_DATA parecem ruído aleatório
- Não é possível ver conteúdo JSON/Base64 do vídeo
- Headers permanecem legíveis (para roteamento)

---

### **Teste 5: Performance com 0% Perdas**

#### Objetivo
Verificar que criptografia não adiciona latência perceptível.

#### Passos

**1. Iniciar Sistema**
```bash
python3 main.py STREAMER1 --tracker 10.0.10.20
python3 main.py C6 --tracker 10.0.10.20
join STREAMER1
```

**2. Monitorizar CPU**
```bash
# Terminal separado
top -p $(pgrep -f "main.py")
```

**3. Observar Vídeo**
- Vídeo deve aparecer em ~300ms
- Não deve ter quebras de áudio
- Áudio/vídeo sincronizados

**4. Verificar Stats**
```
[CLIENTE-BUFFER] Buffer acumulado: 15 pacotes (300ms)
[CLIENTE-STATS] Recebidos: 200 | Decifrados: 200 🔒
```

#### Resultado
✅ **PASSOU** se:
- Latência inicial: ~300ms (igual sem cripto)
- CPU: +2-3% (com AES-NI) ou +5-8% (sem)
- Sem quebras de áudio
- Sync AV perfeita

---

### **Teste 6: Performance com 10% Perdas**

#### Objetivo
Verificar que FEC+NACK continuam funcionando com criptografia.

#### Passos

**1. Aplicar 10% Perdas no Cliente**
```bash
# No nó cliente
sudo tc qdisc add dev eth0 root netem loss 10%
```

**2. Iniciar Stream**
```bash
python3 main.py STREAMER1 --tracker 10.0.10.20
python3 main.py C6 --tracker 10.0.10.20
join STREAMER1
```

**3. Observar Adaptação**
```
[ADAPTAÇÃO] Perdas: 9.2% | Buffer: 300ms -> 650ms
[CLIENTE-FEC] Gap 5: FEC OK=4 | FEC FAIL=1 | Perdido=0
[CLIENTE-STATS] Recebidos: 200 | Decifrados: 200 🔒
```

**4. Verificar Vídeo**
- Buffer deve adaptar para 650ms
- FEC deve recuperar ~95% das perdas
- Zero quebras visuais

**5. Remover Perdas**
```bash
sudo tc qdisc del dev eth0 root
```

#### Resultado
✅ **PASSOU** se:
- FEC recupera perdas normalmente
- Sistema adapta buffer automaticamente
- Zero quebras com 10% perdas
- Decifrados = Recebidos (nenhuma falha)

---

### **Teste 7: Overhead de Banda**

#### Objetivo
Medir aumento de banda com criptografia.

#### Passos

**1. Capturar Tráfego**
```bash
# No streamer
sudo iftop -i eth0 -f "dst port 50000"
```

**2. Stream Sem Criptografia (baseline)**
```bash
# Desabilitar temporariamente (comentar linha no código)
# node.security.enable(stream_key)
```
Observar: **~450 kbps**

**3. Stream Com Criptografia**
```bash
# Reabilitar
```
Observar: **~470 kbps**

#### Resultado
Overhead esperado: **+5% (20-30 kbps)**

✅ **PASSOU** se overhead < 10%

---

## 🔍 Testes Avançados

### **Teste 8: Brute Force Attack (Simulado)**

#### Objetivo
Demonstrar segurança da chave.

#### Conceito
```python
# Tentativa de brute force em chave de 256 bits
tentativas_por_segundo = 1_000_000_000  # 1 bilhão/s
total_chaves = 2 ** 256

anos_necessarios = total_chaves / tentativas_por_segundo / (365.25 * 24 * 3600)
print(f"Tempo para quebrar: {anos_necessarios:.2e} anos")
# Resultado: ~3.67 × 10^56 anos (idade do universo: 1.38 × 10^10 anos)
```

---

### **Teste 9: Man-in-the-Middle (MITM) Attack**

#### Objetivo
Verificar proteção contra modificação de pacotes.

#### Passos

**1. Capturar Pacote Cifrado**
```bash
sudo tcpdump -i eth0 -w capture.pcap udp port 50000
```

**2. Modificar Pacote**
```python
# Ler pacote capturado
with open('capture.pcap', 'rb') as f:
    packet = f.read()

# Modificar 1 byte do ciphertext
modified = bytearray(packet)
modified[100] ^= 0xFF  # Flip bits

# Reenviar
# (simular injeção)
```

**3. Resultado Esperado**
```
[SECURITY] Falha ao decifrar: MAC check failed
[SECURITY] Falha autenticação pacote seq=42
```

✅ **PASSOU** se pacote modificado é rejeitado.

---

### **Teste 10: Replay Attack**

#### Objetivo
Verificar que pacotes antigos não são aceitos.

#### Conceito
Graças ao IV único por pacote e sequências no header:
- Mesmo conteúdo cifrado duas vezes = ciphertexts diferentes
- Sequências antigas descartadas pelo jitter buffer
- Replay não causa impacto

---

## 📊 Métricas de Sucesso

### Performance Esperada

| Métrica | Sem Cripto | Com Cripto | Diferença |
|---------|-----------|-----------|-----------|
| Latência inicial | 300ms | 300-301ms | +0-1ms |
| Throughput | 439 kbps | 462 kbps | +5% |
| CPU (streamer) | 15% | 17% | +2% |
| CPU (cliente) | 12% | 14% | +2% |
| Quebras (0%) | Zero | Zero | Igual |
| Quebras (10%) | Zero | Zero | Igual |
| Recovery FEC | 95% | 95% | Igual |

### Logs de Sucesso

**Streamer**:
```
[SECURITY] Criptografia AES-256-GCM ativada ✓
[STREAMER-STATS] Cifrados: 200 pacotes 🔒
[STREAMER-ADAPT] Perdas: 9% | Pacing: 3ms->5ms | FEC: 1x->2x
```

**Cliente**:
```
[SECURITY] Criptografia AES-256-GCM ativada ✓
[CLIENTE-INIT] Criptografia AES-256-GCM: ATIVA 🔒
[CLIENTE-STATS] Recebidos: 200 | Decifrados: 200 🔒
[ADAPTAÇÃO] Perdas: 9.2% | Buffer: 300ms -> 650ms
```

---

## 🐛 Troubleshooting

### Problema 1: "ModuleNotFoundError: No module named 'cryptography'"

**Solução**:
```bash
pip3 install cryptography
# ou
pip3 install --user cryptography
```

---

### Problema 2: "Falha autenticação" constante

**Causas Possíveis**:
1. **Chaves diferentes** entre streamer e cliente
2. **Pacotes corrompidos** na rede
3. **Versão incompatível** da biblioteca

**Solução**:
```bash
# Verificar chaves
echo $STREAM_KEY  # Deve ser igual em ambos

# Verificar versão
pip3 show cryptography
# Mínimo: 41.0.0

# Reinstalar
pip3 uninstall cryptography
pip3 install cryptography
```

---

### Problema 3: CPU alto (>30%)

**Causa**: Sem aceleração hardware AES-NI

**Verificar**:
```bash
lscpu | grep -i aes
# Se não aparecer "aes", CPU não tem AES-NI
```

**Solução**: Normal em CPUs antigas. Performance ainda aceitável.

---

### Problema 4: Stream não aparece

**Causa**: Chaves incompatíveis ou criptografia não ativada em um lado

**Debug**:
```bash
# Verificar logs de ambos os lados
# Streamer deve mostrar: "Cifrados: X"
# Cliente deve mostrar: "Decifrados: X"

# Se streamer cifra mas cliente não decifra:
[SECURITY] Pacote cifrado recebido mas criptografia não está ativa!
```

**Solução**: Garantir que ambos têm criptografia ativa com mesma chave.

---

## ✅ Checklist Final

Antes de considerar testes completos, verificar:

- [ ] `pip3 install cryptography` executado
- [ ] Ambos os nós mostram "Criptografia AES-256-GCM ativada"
- [ ] Streamer mostra "Cifrados: X pacotes 🔒"
- [ ] Cliente mostra "Decifrados: X pacotes 🔒"
- [ ] Stream funciona com 0% perdas (sem quebras)
- [ ] Stream funciona com 10% perdas (zero quebras visuais)
- [ ] FEC continua recuperando perdas
- [ ] Overhead de banda < 10%
- [ ] CPU aumenta apenas 2-5%
- [ ] Wireshark mostra payloads ilegíveis
- [ ] Chaves diferentes causam falha de autenticação

---

## 📚 Próximos Passos

Após testes básicos, considerar:

1. **Rotação de Chaves**: Trocar chave a cada X minutos
2. **Múltiplos Clientes**: Testar com 5+ clientes simultâneos
3. **Stress Test**: 20% perdas + 10 clientes
4. **Latência Extrema**: Simular 500ms RTT
5. **Ataque Ativo**: Tentar modificar pacotes na rede

---

**Documento gerado em**: 13 de Dezembro de 2025  
**Versão do Sistema**: Criptografia AES-256-GCM v1.0
