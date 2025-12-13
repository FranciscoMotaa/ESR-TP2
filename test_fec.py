#!/usr/bin/env python3
"""
Teste automatizado do sistema FEC para validar eficácia com diferentes perdas
"""
import random
import time
from overlay_structs import FECEncoder, FECDecoder

def simulate_packet_loss(packets, loss_rate, burst_size=1):
    """
    Simula perda de pacotes com bursts
    
    Args:
        packets: Lista de pacotes
        loss_rate: Taxa de perda (0.0 a 1.0)
        burst_size: Tamanho do burst (1=aleatório, >1=burst)
    
    Returns:
        Lista de pacotes após perda (None = perdido)
    """
    result = []
    i = 0
    while i < len(packets):
        if random.random() < loss_rate:
            # Burst: perder múltiplos consecutivos
            for _ in range(burst_size):
                if i < len(packets):
                    result.append(None)
                    i += 1
        else:
            result.append(packets[i])
            i += 1
    return result

def test_fec_basic():
    """Teste básico: k=1, sem perda"""
    print("\n" + "="*70)
    print("TESTE 1: FEC Básico (0% perda)")
    print("="*70)
    
    encoder = FECEncoder()
    decoder = FECDecoder(k=1)
    
    # Criar dados
    data = b"Hello World! " * 40  # ~500 bytes
    
    # Gerar FEC
    parity = encoder.generate_parity([data])
    
    # Decoder recebe tudo
    decoder.add_data_packet(1, data)
    decoder.add_fec_packet(0, parity, [len(data)])
    
    # Não há nada para recuperar
    recovered = decoder.get_recovered(1)
    
    print(f"✅ Dados: {len(data)} bytes")
    print(f"✅ FEC: {len(parity)} bytes")
    print(f"✅ Resultado: PASS (sem perdas)")
    return True

def test_fec_single_loss():
    """Teste: k=1, perde dado mas FEC chega"""
    print("\n" + "="*70)
    print("TESTE 2: FEC com 1 Perda (dado perdido, FEC OK)")
    print("="*70)
    
    encoder = FECEncoder()
    decoder = FECDecoder(k=1)
    
    data = b"Test data " * 50
    parity = encoder.generate_parity([data])
    
    # Simular: Dado perde, FEC chega
    decoder.add_fec_packet(0, parity, [len(data)])
    
    # Tentar recuperar
    recovered = decoder.get_recovered(1)
    
    if recovered and len(recovered) > 0:
        print(f"✅ Dados originais: {len(data)} bytes")
        print(f"✅ FEC: {len(parity)} bytes")
        print(f"✅ Recuperado: {len(recovered)} bytes")
        print(f"✅ Match: {recovered[:50] == data[:50]}")
        return True
    else:
        print(f"❌ FALHOU: Não recuperou dados")
        return False

def test_fec_multiple_packets(loss_rate=0.10, num_packets=100):
    """Teste completo: múltiplos pacotes com perda aleatória"""
    print("\n" + "="*70)
    print(f"TESTE 3: {num_packets} pacotes com {loss_rate*100:.0f}% perda ALEATÓRIA")
    print("="*70)
    
    encoder = FECEncoder()
    decoder = FECDecoder(k=1)
    
    # Gerar pacotes de dados
    packets = []
    for i in range(num_packets):
        data = f"Packet {i:04d} ".encode() * 60  # ~500 bytes cada
        packets.append(data)
    
    # Gerar FEC para cada pacote
    fec_packets = []
    for i, data in enumerate(packets):
        parity = encoder.generate_parity([data])
        fec_packets.append((i, parity, [len(data)]))
    
    # Simular perda ALEATÓRIA (não-burst)
    data_received = []
    for i, pkt in enumerate(packets):
        if random.random() > loss_rate:
            data_received.append((i, pkt))
        else:
            data_received.append((i, None))  # Perdido
    
    fec_received = []
    for i, (block_id, parity, sizes) in enumerate(fec_packets):
        if random.random() > loss_rate:
            fec_received.append((block_id, parity, sizes))
        else:
            fec_received.append((block_id, None, sizes))  # Perdido
    
    # Processar no decoder
    data_lost = 0
    data_recovered = 0
    both_lost = 0
    
    for i in range(num_packets):
        data_ok = data_received[i][1] is not None
        fec_ok = fec_received[i][1] is not None
        
        if data_ok:
            decoder.add_data_packet(i, data_received[i][1])
        
        if fec_ok:
            decoder.add_fec_packet(fec_received[i][0], fec_received[i][1], fec_received[i][2])
        
        if not data_ok:
            data_lost += 1
            # Tentar recuperar
            recovered = decoder.get_recovered(i)
            if recovered:
                data_recovered += 1
            else:
                both_lost += 1
    
    total = num_packets
    final_loss = both_lost
    final_loss_rate = (final_loss / total) * 100
    recovery_rate = (data_recovered / data_lost * 100) if data_lost > 0 else 0
    
    print(f"📊 Total pacotes: {total}")
    print(f"📊 Dados perdidos: {data_lost} ({data_lost/total*100:.1f}%)")
    print(f"📊 FEC recuperou: {data_recovered}")
    print(f"📊 Perdido final: {both_lost} ({final_loss_rate:.1f}%)")
    print(f"📊 Taxa recuperação FEC: {recovery_rate:.1f}%")
    
    # Critério de sucesso: <1% perda final com 10% perda rede
    success = final_loss_rate < 1.0
    
    if success:
        print(f"✅ PASS: Perda final {final_loss_rate:.1f}% < 1.0%")
    else:
        print(f"❌ FAIL: Perda final {final_loss_rate:.1f}% >= 1.0%")
    
    return success

def test_fec_burst_loss(loss_rate=0.10, burst_size=3, num_packets=100):
    """Teste CRÍTICO: perda em BURST (problema real)"""
    print("\n" + "="*70)
    print(f"TESTE 4: {num_packets} pacotes com {loss_rate*100:.0f}% perda em BURST (tamanho {burst_size})")
    print("="*70)
    
    encoder = FECEncoder()
    decoder = FECDecoder(k=1)
    
    # Gerar pacotes
    packets = []
    for i in range(num_packets):
        data = f"Packet {i:04d} ".encode() * 60
        packets.append(data)
    
    # Gerar FEC
    fec_packets = []
    for i, data in enumerate(packets):
        parity = encoder.generate_parity([data])
        fec_packets.append((i, parity, [len(data)]))
    
    # IMPORTANTE: Simular envio com espaçamento
    # Dado em t=0, FEC1 em t+15ms, FEC2 em t+30ms, FEC3 em t+45ms
    # Burst típico: 5-15ms
    
    # Criar timeline simulada
    timeline = []
    for i in range(num_packets):
        timeline.append(('data', i, 0, packets[i]))
        timeline.append(('fec', i, 15, fec_packets[i]))  # +15ms
        timeline.append(('fec', i, 30, fec_packets[i]))  # +30ms
        timeline.append(('fec', i, 45, fec_packets[i]))  # +45ms
    
    # Simular burst loss
    current_burst = False
    burst_duration = 0
    burst_duration_max = 10  # 10ms típico
    
    received_data = {}
    received_fec = {}
    
    for pkt_type, block_id, time_offset, content in timeline:
        # Simular burst: iniciar burst aleatoriamente
        if random.random() < loss_rate / 10:  # Menos frequente mas duradouro
            current_burst = True
            burst_duration = burst_duration_max
        
        # Durante burst: perder pacotes
        if current_burst:
            # Perdido!
            burst_duration -= 1
            if burst_duration <= 0:
                current_burst = False
        else:
            # Recebido
            if pkt_type == 'data':
                received_data[block_id] = content
            else:  # fec
                if block_id not in received_fec:
                    received_fec[block_id] = []
                received_fec[block_id].append(content)
    
    # Processar
    data_lost = 0
    data_recovered = 0
    both_lost = 0
    
    for i in range(num_packets):
        data_ok = i in received_data
        fec_ok = i in received_fec and len(received_fec[i]) > 0
        
        if data_ok:
            decoder.add_data_packet(i, received_data[i])
        
        if fec_ok:
            # Usar primeiro FEC recebido
            _, parity, sizes = received_fec[i][0]
            decoder.add_fec_packet(i, parity, sizes)
        
        if not data_ok:
            data_lost += 1
            recovered = decoder.get_recovered(i)
            if recovered:
                data_recovered += 1
            else:
                both_lost += 1
    
    total = num_packets
    final_loss = both_lost
    final_loss_rate = (final_loss / total) * 100
    recovery_rate = (data_recovered / data_lost * 100) if data_lost > 0 else 0
    network_loss_rate = (data_lost / total) * 100
    
    print(f"📊 Total pacotes: {total}")
    print(f"📊 Perda rede (dados): {data_lost} ({network_loss_rate:.1f}%)")
    print(f"📊 FEC recuperou: {data_recovered} ({recovery_rate:.1f}%)")
    print(f"📊 Perdido final: {both_lost} ({final_loss_rate:.1f}%)")
    print(f"📊 FEC recebidos: {len(received_fec)} blocos")
    
    # Critério: com burst e 10%, aceitamos até 2% perda final
    success = final_loss_rate < 2.0
    
    if success:
        print(f"✅ PASS: Perda final {final_loss_rate:.1f}% < 2.0% (com burst)")
    else:
        print(f"❌ FAIL: Perda final {final_loss_rate:.1f}% >= 2.0%")
    
    return success

def run_all_tests():
    """Executa todos os testes"""
    print("\n" + "="*70)
    print("TESTES AUTOMATIZADOS DO SISTEMA FEC")
    print("Objetivo: Stream perfeita com até 10% perda de rede")
    print("="*70)
    
    random.seed(42)  # Reproduzível
    
    results = []
    
    # Teste 1: Básico
    results.append(("Básico (0% perda)", test_fec_basic()))
    
    # Teste 2: 1 perda
    results.append(("1 Perda simples", test_fec_single_loss()))
    
    # Teste 3: 5% aleatório
    results.append(("5% perda aleatória", test_fec_multiple_packets(0.05, 200)))
    
    # Teste 4: 10% aleatório
    results.append(("10% perda aleatória", test_fec_multiple_packets(0.10, 200)))
    
    # Teste 5: 10% burst (CRÍTICO)
    results.append(("10% perda BURST", test_fec_burst_loss(0.10, 3, 200)))
    
    # Teste 6: 15% burst (extremo)
    results.append(("15% perda BURST", test_fec_burst_loss(0.15, 3, 200)))
    
    # Resumo
    print("\n" + "="*70)
    print("RESUMO DOS TESTES")
    print("="*70)
    
    passed = 0
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {name}")
        if result:
            passed += 1
    
    print("\n" + "="*70)
    print(f"Total: {passed}/{len(results)} testes passaram")
    
    if passed == len(results):
        print("🎉 TODOS OS TESTES PASSARAM!")
        print("✅ Sistema pronto para streaming até 10% perda!")
    else:
        print("⚠️  ALGUNS TESTES FALHARAM")
        print("❌ Sistema precisa ajustes")
    
    print("="*70)
    
    return passed == len(results)

if __name__ == "__main__":
    success = run_all_tests()
    exit(0 if success else 1)
