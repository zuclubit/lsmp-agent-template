# HALCON — s-agent
<!-- Generado por `halcon /init` · 2026-03-17 -->

> **⚐ Salud del proyecto: 39/100** — NECESITA ATENCIÓN  
> **◈ Agente listo: 50/100** — BÁSICO  
> **◈ Entorno compatible: 90/100** — ÓPTIMO

## Proyecto
- **Nombre**: `s-agent`
- **Tipo**: Python
- **Versión**: 1.0.0
- **Descripción**: Enterprise-grade Legacy to Salesforce migration agent following Clean Architecture and DDD principles.
- **Licencia**: MIT

## Arquitectura
- **Estilo**: layered
- **Complejidad estimada**: Media (30/100)

## Infraestructura
- ✗ **CI/CD**: No detectado
- ✓ **Tests**: Detectados (~45% cobertura estimada)
- ✗ **Security Policy**: Sin SECURITY.md

## Entorno de Ejecución
- **OS**: macos aarch64
- **CPU**: 10 cores
- **RAM**: 24.0 GB
- **Disco libre**: 39.0 GB
- ✓ **GPU**: Disponible

## Herramientas del Sistema
- **git**: 2.39.5
- **rustc**: 1.90.0
- **cargo**: 1.90.0
- **node**: v24.9.0
- **python**: 3.9.6
- **docker**: 28.5.1,
- **make**: GNU Make 3.81
- **Infra tools**: terraform

## Contexto IDE
- ✓ **LSP / Dev Gateway**: Conectado (puerto 5758)

## Archivos de Contexto AI Detectados
> Este proyecto tiene instrucciones para múltiples asistentes AI.

- `CLAUDE.md` — Claude Code

## Capacidades del Agente
- **MCP Servers**: 1 activos — filesystem
- **Subsistemas activos**: Reasoning, Orchestration, Multimodal, Plugins
- **Tools disponibles**: 7 herramientas

## Estructura
```
s-agent/
├── adapters/
├── agents/
├── analysis/
├── application/
├── architecture/
├── ci-cd/
├── config/
├── context-servers/
├── data/
├── docs/
├── domain/
├── halcon/
├── infrastructure/
├── integrations/
├── migration/
├── monitoring/
├── salesforce/
├── scripts/
├── security/
├── skills/
├── tests/
└── tools/
```

## Inteligencia de Lenguajes
- **Lenguaje primario**: Python
- **Lenguajes secundarios**: HCL/Terraform, Shell, CSS, HTML
- ◈ **Repositorio poliglota**: múltiples lenguajes de producción
- **Distribución**: Python (154), HCL/Terraform (9), Shell (6), CSS (1), HTML (1), JavaScript (1)
- **Escala**: Small (≤ 500 archivos) · 337 archivos escaneados
- **LOC estimadas**: ~30330 líneas

## Arquitectura Distribuida
- **Patrones**: DDD

## Dashboard de Calidad (10 Métricas)
| Métrica | Puntuación | Nivel |
|---|---|---|
| Salud del Proyecto | 39/100 | ⚐ Bajo |
| Listo para Agente | 50/100 | ⚐ Bajo |
| Compatibilidad Entorno | 90/100 | ◈ Alto |
| Calidad de Arquitectura | 75/100 | ◇ Medio |
| Escalabilidad | 45/100 | ⚐ Bajo |
| Mantenibilidad | 73/100 | ◇ Medio |
| Deuda Técnica | 33/100 | ◇ Moderado |
| Developer Experience | 70/100 | ◇ Medio |
| Preparación IA | 80/100 | ◈ Alto |
| Madurez Distribuida | 10/100 | ⚐ Bajo |

## Matriz de Capacidades
| Capacidad | Detectada | Estado | Riesgo |
|---|---|---|---|
| Tests | ✓ | Cobertura baja | Bajo |
| CI/CD | ✗ | No configurado | Alto |
| Containers | ✗ | Sin containerización | Medio |
| Security Policy | ✗ | Sin política | Medio |
| Dep Auditing | ✗ | Sin auditoría | Medio |
| Observability | ✗ | Sin observability | Bajo |
| Message Broker | ✗ | No detectado | Bajo |
| Service Mesh | ✗ | Sin mesh | Bajo |

## Configuración de Agente Sugerida
> **Análisis**: polyglot repository, Data/AI project — multimodal analysis useful

```bash
halcon chat --full --expert
```

- **Modelo sugerido**: fast (Haiku / GPT-4o-mini — respuesta rápida, proyecto simple)
- ◈ **Análisis multimodal**: Útil para proyecto Data/AI

## Riesgos Detectados
- ⚐ No CI/CD system detected
- ⚐ Low estimated test coverage (~45%)
- ⚐ No git repository found
- ⚐ No SECURITY.md found

## Recomendaciones
1. Add GitHub Actions or GitLab CI for automated testing
2. Increase test coverage to at least 60%
3. Initialize a git repository for version control
4. Add SECURITY.md with responsible disclosure policy

## Oportunidades de Optimización
1. Integrar con IDE: instalar extensión HALCON para VSCode/Cursor para LSP
2. Añadir pipeline CI/CD (GitHub Actions / GitLab CI) para automatización
3. Crear SECURITY.md con política de divulgación de vulnerabilidades

## Instrucciones para el Agente

Eres **HALCON**, un asistente de ingeniería autónomo para el proyecto `s-agent`.

### Identidad
- Responde siempre en el idioma del usuario (ES ↔ EN)
- Sé conciso y orientado a la acción — sin relleno
- Usa las convenciones y estilo del proyecto existente

### Flujo de trabajo
- Lee los archivos relevantes ANTES de modificarlos
- Prefiere editar archivos existentes sobre crear nuevos
- Ejecuta las pruebas después de cambios significativos
- Usa `git status`/`git diff` para entender el árbol de trabajo

### Comandos clave
```bash
python -m pytest          # Tests
pip install -e .          # Install editable
python -m ruff check .    # Linting
```

### Prioridades
1. Seguridad y correctitud
2. Rendimiento y eficiencia
3. Legibilidad y mantenibilidad
4. Tests de regresión para cada fix

---
*Generado por `halcon /init` · 2026-03-17*  
*Análisis: 3.3s · 143 archivos · 28 herramientas*
*Detección recursos: 3234ms · Entorno: 3234ms · IDE: 0ms · HICON: 594ms*
