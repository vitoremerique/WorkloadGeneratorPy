# WorkloadGeneratorPy

Gerador de carga sintética de **CPU**, **RAM** e **disco (IOPS)**, em Python.

Todas as cargas rodam em **threads do próprio processo**, sem processos filhos. Assim, todo o consumo aparece no PID do gerador, o que facilita monitorar a carga e medir o overhead de ferramentas de monitoração.

Usa só a biblioteca padrão do Python (3.8 ou mais novo). Roda apenas em Linux.

## Uso

```bash
python3 workload_generator.py [opções]
```

| Opção | Descrição | Padrão |
|-------|-----------|--------|
| `-c, --cores <n>` | Número de núcleos usados pela carga de CPU | `1` |
| `-u, --cpu <%>` | Percentual de uso em cada núcleo (0–100) | — |
| `-r, --ram <GB>` | Memória a alocar, em GB (aceita fração) | — |
| `-i, --iops <n>` | Operações de disco por segundo | — |
| `-w, --readwrite <%>` | Percentual de leituras; o restante é escrita | `50` |
| `-t, --time <s>` | Duração da carga, em segundos | `60` |
| `--io-file <caminho>` | Arquivo usado pela carga de disco | `teste_io.dat` |
| `--io-size <GB>` | Tamanho do arquivo de disco | `4` |
| `-h, --help` | Mostra a ajuda | |

Só as cargas cujas opções forem passadas são geradas. `--cores` sozinho não gera carga; use-o junto com `--cpu`. `Ctrl+C` encerra antes do tempo.

Ao terminar, o gerador mostra o que foi obtido (IOPS reais, blocos sha256 por segundo e memória alocada). Esses números servem para comparar execuções com e sem monitoração.

## Como cada carga é gerada

- **CPU:** uma thread por núcleo. A cada janela de 10 ms, a thread calcula sha256 durante o percentual pedido e dorme no restante. O sha256 roda em C e libera o GIL, então as threads ocupam núcleos diferentes ao mesmo tempo.
- **RAM:** aloca a memória, escreve em todas as páginas e a mantém ocupada até o fim. Depois de alocada, essa carga não consome CPU.
- **Disco:** leituras e escritas aleatórias de 4K com `O_DIRECT` (sem cache de páginas), uma operação por vez. As operações são espaçadas para atingir o IOPS pedido; se o disco não acompanhar, a taxa fica no máximo possível.

## Exemplos

```bash
python3 workload_generator.py --cores 2 --cpu 50 --time 30              # só CPU
python3 workload_generator.py --ram 3 --time 30                         # só memória
python3 workload_generator.py --iops 1000 --readwrite 70 --time 30      # só disco
python3 workload_generator.py -c 4 -u 80 -r 4 -i 5000 -w 70 -t 60       # tudo junto
```

## Observações

- **Arquivo de teste:** a carga de disco cria `teste_io.dat` (4 GB por padrão) e o preenche antes de começar a contar o tempo. Ele é reaproveitado nas próximas execuções. Apague ao terminar: `rm -f teste_io.dat`.
- **I/O direto:** alguns sistemas de arquivos, como o `tmpfs`, não aceitam `O_DIRECT`.
- **Memória:** não peça mais RAM do que a máquina tem livre, senão o sistema pode usar swap ou acionar o OOM killer.
- **Monitorar a carga:** `top -p <PID>`, `pidstat -u -r -d -p <PID> 1` ou `/proc/<PID>/io`. O PID é mostrado ao iniciar.
