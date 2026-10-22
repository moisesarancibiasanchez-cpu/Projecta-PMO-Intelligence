# Projecta PMO Intelligence

**Projecta PMO Intelligence** es una plataforma de inteligencia para PMO que convierte reuniones, correos, tickets, documentos y planes de proyecto en decisiones trazables, tareas ejecutables y reportes ejecutivos.

> **Convierte datos, decisiones y compromisos en control ejecutivo.**

---

## 🚀 Descripción

Projecta PMO Intelligence actúa como una capa inteligente sobre los sistemas que ya utiliza una organización: Jira, Azure DevOps, MS Project, Oracle APEX, Teams, SharePoint, correo y bases de datos corporativas.

Su objetivo es cerrar la brecha entre lo que ocurre en el día a día de un proyecto y lo que queda registrado, controlado y visible para la PMO y la jefatura.

El sistema permite:

- Capturar información desde reuniones, correos, documentos y tickets.
- Identificar acuerdos, decisiones, riesgos, dependencias y compromisos.
- Generar actas, informes de avance, historias de usuario y reportes ejecutivos.
- Crear o actualizar tareas y tickets previa validación humana.
- Mantener trazabilidad completa desde el acuerdo original hasta su ejecución.
- Consolidar el estado de proyectos y portafolios en dashboards ejecutivos.

---

## ✨ Funcionalidades principales

### 🧠 Captura inteligente
- Ingesta de transcripciones, grabaciones, correos, documentos y tickets.
- Procesamiento de archivos Word, PDF, Excel y XML de MS Project.
- Búsqueda semántica sobre actas, documentos, tareas y evidencias.
- Integración con Microsoft Teams, SharePoint, Jira, Azure DevOps y sistemas internos.

### 📝 Documentación PMO
- Generación de actas de reunión y minutas ejecutivas.
- Detección de acuerdos, responsables, fechas y compromisos.
- Creación de informes de avance y reportes semanales.
- Generación de historias de usuario y criterios de aceptación.
- Elaboración de matrices de riesgos, supuestos y dependencias.
- Preparación de presentaciones para comités y reportes ejecutivos.

### ✅ Gestión de compromisos
- Conversión de acuerdos en tareas o tickets.
- Asignación de responsables, fechas y prioridades.
- Alertas por compromisos vencidos o próximos a vencer.
- Seguimiento del estado de avance por proyecto y responsable.

### ⚠️ Gestión de riesgos
- Identificación de riesgos, bloqueos y dependencias.
- Clasificación por impacto, probabilidad y criticidad.
- Registro de planes de mitigación y responsables.
- Alertas tempranas ante desvíos o riesgos sin tratamiento.

### 📊 Inteligencia de portafolio
- Dashboards por proyecto, programa y portafolio.
- Semáforos de estado, avance, riesgo y cumplimiento.
- Análisis de backlog, antigüedad de tareas y cuellos de botella.
- Comparación entre planificación y ejecución real.
- Visión consolidada para comités y gerencia.

### 🔍 Trazabilidad y auditoría
- Registro de la fuente original de cada decisión o tarea.
- Versionamiento de documentos y cambios aprobados.
- Historial de acciones realizadas por usuarios y agentes IA.
- Control de accesos, permisos y evidencia auditable.

---

## 🧩 Arquitectura conceptual

```text
+--------------------------------------------------+
|              Capa de presentación                 |
|   Oracle APEX · Web · Teams · Slack · Dashboards  |
+--------------------------------------------------+
                         |
+--------------------------------------------------+
|             Capa de orquestación IA               |
|   Agentes · RAG · LLM Router · Workflows          |
+--------------------------------------------------+
                         |
+--------------------------------------------------+
|                Capa de dominio PMO                |
|  Proyectos · Tareas · Riesgos · Decisiones · HU   |
+--------------------------------------------------+
                         |
+--------------------------------------------------+
|               Capa de integración                 |
| Jira · Azure DevOps · MS Project · Teams · Drive  |
+--------------------------------------------------+
                         |
+--------------------------------------------------+
|            Capa de datos y seguridad              |
| Base de datos · Vector Store · Logs · Permisos    |
+--------------------------------------------------+
```

---

## 🛠️ Stack técnico propuesto

| Capa | Tecnología |
|---|---|
| Frontend y workflows | Oracle APEX |
| Base de datos | Oracle Database |
| Backend de IA | Python · FastAPI |
| Procesamiento documental | Python · Azure Document Intelligence |
| Búsqueda semántica | Azure AI Search / pgvector |
| Modelos de IA | Azure OpenAI · OpenAI · Claude · Gemini |
| Integraciones | Azure Logic Apps · Azure Functions · .NET |
| Almacenamiento documental | SharePoint · Azure Blob Storage · OneDrive |
| Identidad y seguridad | Microsoft Entra ID |
| Observabilidad | Application Insights · Logs estructurados |

---

## 🔄 Flujo de trabajo típico

1. El sistema ingiere una reunión, correo, documento o ticket.
2. La IA extrae acuerdos, riesgos, decisiones y compromisos.
3. Se genera un borrador de acta, tareas o informe.
4. El responsable revisa y aprueba el contenido.
5. Projecta crea o actualiza tareas en el sistema correspondiente.
6. Se registra la evidencia y la trazabilidad.
7. Se actualizan los dashboards y reportes ejecutivos.
8. Se envían alertas y recordatorios según configuración.

---

## 🧪 MVP inicial

El primer módulo recomendado es **Projecta Meetings**:

- Cargar una transcripción o grabación de reunión.
- Generar acta estructurada.
- Detectar compromisos, responsables y fechas.
- Permitir corrección y aprobación manual.
- Crear tareas en una base PMO.
- Enviar resumen por correo o Teams.
- Visualizar compromisos y riesgos en un dashboard básico.

---

## 📈 KPIs sugeridos

- Horas ahorradas por jefe de proyecto al mes.
- Porcentaje de reuniones con acta generada y aprobada.
- Porcentaje de compromisos con responsable y fecha.
- Reducción de compromisos vencidos.
- Tiempo de preparación de informes de avance.
- Porcentaje de riesgos con plan de mitigación.
- Reducción del aging del backlog.
- Exactitud de tareas y acuerdos extraídos por IA.

---

## 🔐 Principios de diseño

- **Human-in-the-loop:** la IA propone, el usuario valida y el sistema registra.
- **Trazabilidad por defecto:** cada resultado conserva su fuente y evidencia.
- **Integración antes que reemplazo:** Projecta convive con las herramientas existentes.
- **Seguridad corporativa:** control de accesos, cifrado, auditoría y residencia de datos.
- **Modularidad:** cada capacidad puede implementarse y venderse como módulo independiente.

---

## 🗺️ Roadmap

| Fase | Entregable |
|---|---|
| Fase 1 | Actas, compromisos y dashboard básico |
| Fase 2 | Riesgos, informes semanales y alertas |
| Fase 3 | Integración con Jira, Azure DevOps, Teams y SharePoint |
| Fase 4 | Conciliación con MS Project y planes de proyecto |
| Fase 5 | Inteligencia de portafolio y reportes ejecutivos |
| Fase 6 | Forecasting, calidad de datos y auditoría avanzada |

---

## 🤝 Contribuciones

Las contribuciones son bienvenidas. Para proponer cambios:

1. Crea un fork del repositorio.
2. Crea una rama: `feature/nueva-funcionalidad`.
3. Realiza los cambios y agrega pruebas cuando corresponda.
4. Abre un Pull Request con una descripción clara.

---

## 📄 Licencia

Este proyecto se distribuye bajo la licencia [MIT](LICENSE).

---

## 📬 Contacto

Para consultas, ideas o colaboraciones, abre un [Issue](../../issues) en este repositorio.
