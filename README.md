# neuro

Собственный майнер Pearl (PRL), разрабатываемый автономным DSH-конвейером.
Первый целевой GPU — RTX 5080 Laptop 16 GB под Windows; управляющий DSH работает
в Linux и позднее запускает проверки на ноутбуке через SSH.

**Подготовка: итерация 1 из 10.** Сейчас это исполняемая основа разработки:
зафиксированные исходники, проверки математики и протокола, первый GPU-пробник,
учёт стоимости и инструменты проверки экспериментов. Собственный майнер с
проверенными на пуле шарами пока не готов. Измеренного GPU-хешрейта нет.

## Принятые решения

- **Основа:** [CPPminer](https://github.com/1640675651/CPPminer), commit
  `5b68506af5e6433ee5c25aa9e6fbb00b537590aa`, подключённый как неизменяемый submodule.
  Повторно используем протокол, управляющие компоненты, CPU-эталон и интерфейсы
  Rust proof. Собственный код проекта — MIT; зависимости сохраняют свои лицензии.
- **GPU:** CUDA C++17, при необходимости inline PTX. Нынешнее CUDA-ядро CPPminer
  ориентировано на старые GPU и не считается готовой основой производительности
  RTX 50. Для SM120 будет отдельное ядро с точными промежуточными результатами.
- **Исполнитель DSH:** предварительно `openai/gpt-6.1-sol`; стоимость считаем
  без скидки за кеш. Первый дешёвый кандидат на сравнение — `minimax/minimax-m3`.
- **Автономность:** модель пишет, собирает, проверяет и исправляет код сама.
  Эксперт CUDA со стороны владельца не предполагается. Проект использует
  существующий DSH, а не разрабатывает ещё один агентский фреймворк.

Обоснование: [архитектура](docs/architecture.md),
[проверка исходников](docs/upstream-audit.md),
[модели и цены](docs/model-selection.md).

## Быстрый старт проверок на Linux

Нужны Git, Python 3.11+ и GCC с C++17/LTO. Python-зависимостей нет.

```bash
git clone --recurse-submodules https://github.com/alinescafs3mp-afk/neuro.git
cd neuro
python3 tools/neuroctl.py doctor
python3 -m unittest discover -s tests -v
python3 tools/neuroctl.py cpu-check --output artifacts/cpu-check.json
```

Если репозиторий уже получен без зависимостей:

```bash
git submodule update --init --recursive
python3 tools/neuroctl.py source-check
```

`cpu-check` компилирует и запускает четыре отдельных исполняемых файла:

1. Проверки аргументов CPPminer.
2. Проверки разбора заданий, ответов и очереди подтверждений пула.
3. Независимое сравнение накопительного GEMM/XOR с CPU-кодом CPPminer.
4. Проверку размещения INT8-данных в регистрах будущего GPU-пробника.

Эти команды не запускают майнинг, не подключаются к пулу и не вызывают API моделей.

Альтернативная сборка через CMake:

```bash
cmake -S . -B build/cmake -DCMAKE_BUILD_TYPE=Release
cmake --build build/cmake -j 2
ctest --test-dir build/cmake --output-on-failure
```

## Первый GPU-пробник

`native/cuda/mma_probe.cu` проверяет INT8 Tensor Core instruction, раскладку
регистров и накопление результата на двух последовательных шагах. Это небольшая
диагностика перед разработкой полного ядра, без пула и proof.

```bash
cmake -S . -B build/cuda -DNEURO_ENABLE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120
cmake --build build/cuda --config Release
```

Выполнение требует настоящего GPU и совместимого драйвера. Команды запуска,
проверяемые случаи и границы результата: [GPU-пробник](docs/gpu-probe.md).
На Windows нужен поддерживаемый CUDA host compiler / Visual Studio Build Tools;
Linux/GCC-проверка полной математики не объявляется проверенной на Windows.

## Стоимость и эксперименты

Посчитать стоимость одного сохранённого API usage по тарифам владельца:

```bash
python3 tools/neuroctl.py cost --model openai/gpt-6.1-sol --usage examples/usage-openai-uncached.json
python3 tools/neuroctl.py cost --model minimax/minimax-m3 --usage examples/usage-openai-cached.json
```

Первый пример даст $0.02079 за 64k входных и 2k выходных токенов.
Файлы в `examples/` синтетические; это проверка арифметики, не оплаченные запросы.
Отсутствие cache metadata не превращается в кеш-хит. Неполный usage даёт
неуспешный exit code. Реальная интеграция учёта и ограничения расходов с DSH
ещё предстоит; текущая утилита не перехватывает его API-вызовы.

Один локальный процесс с таймаутом и ограниченными логами:

```bash
python3 tools/neuroctl.py run --stage verification --timeout 60 --output artifacts/run.json -- ./build/cpu/cppminer_math_probe
```

Сравнение результатов требует независимой проверки корректности и отдельных
повторных измерений. Синтетический пример намеренно отклоняется:

```bash
python3 tools/neuroctl.py compare --candidate examples/experiment-result.json --baseline examples/experiment-result.json
```

[Контракт эксперимента](docs/experiment-contract.md) объясняет поля и ограничения.
Процесс с exit 0 сам по себе не доказывает корректность майнинга или ускорение.

## Продолжение работы

Текущая точка входа: [state/current.json](state/current.json).
Отчёт первой итерации: [docs/iterations/01.md](docs/iterations/01.md).
План десяти итераций: [docs/roadmap.md](docs/roadmap.md).

В десятой итерации будет подготовлен самодостаточный архив для распаковки в
`/home/jericho/neuro/start/` и стартовая директива DSH. Обычный ZIP GitHub не
включает содержимое submodule и не заменяет этот будущий архив.

Приватные настройки копируются из `configs/local.example.json` в игнорируемый
`configs/local.json`; API/SSH/GitHub-ключи в репозиторий не помещаются.
