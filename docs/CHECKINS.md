# Check-ins contextuales (opt-in)

Implementación funcional del vertical en fixtures, **no migración live aceptada**.
No se ha cambiado instalación, perfil, planner ni entrega existente.

## Flujo

El ticker existente ejecuta `Worker.tick`, que también revisa un check-in por día
local. `checkin_days` y `checkin_responses` viven en el mismo SQLite que los items,
presupuestos y outbox. No se crea otro scheduler, cron diario ni job por pregunta.
Un lock acotado serializa preparación/revalidación; una clave principal+día y el
presupuesto de outbox limitan a un intento diario. UNKNOWN consume ese intento.
Un recibo de transporte no es respuesta humana ni prueba de lectura.

1. Inventariar integraciones con `hermes continuity capabilities` y autorizar los
   adaptadores de lectura descritos en INTEGRATIONS.md. No se exige ninguna marca,
   vault, skill ni sistema de notas. Una integración no soportada queda visible.
2. Dar consentimiento separado a lecturas del workflow, inferencia y envío a un
   destino propio exacto. Shadow/pausa no leen ni infieren para este workflow.
3. Antes de inferir, adquirir realmente las fuentes seleccionadas mediante
   adaptadores acotados. Los diez roles de evidencia siempre aparecen con
   OK/PARTIAL/UNAVAILABLE, referencias, digest y momento de observación.
4. Elegir el primer intervalo permitido entre 09:00 y 21:00 en la zona configurada,
   fuera de eventos y con margen de 15 minutos. Nunca se entrega automáticamente
   a las 09:00: hay 30 minutos iniciales de observación, y se pospone 30 minutos
   desde un hilo humano reciente. Un evento autorizado que empieza en los próximos
   90 minutos prefiere su final+15 minutos. Es una heurística determinista de
   actividad/agenda, no una inferencia demostrada de pertinencia semántica.
   Cada calendario seleccionado debe
   gatear elegibilidad, estar completo y cubrir esa ventana; si falla, no se redacta.
   El ticker vuelve a evaluar cuando haya un hueco: no programa un segundo job.
5. Sintetizar sobre el paquete adquirido, no pedir al LLM sin herramientas que
   finja leer. Validar esquema cerrado, pregunta, hasta tres frases, cita literal
   presente en fuente y mensaje, y ausencia de sintaxis de adjuntos/rutas.
   El contrato léxico de rutas es conservador: se rechaza cualquier `/` o `\`
   (POSIX absoluto, `~/`, `~usuario/`, `./`, `../`, relativo con directorios,
   Windows con unidad, raíz o UNC), y un prefijo de unidad `[A-Za-z]:` seguido
   inmediatamente por un carácter no blanco (`C:archivo.md`). También puede
   rechazar ratios o prosa con separadores; no detecta nombres aislados,
   representaciones codificadas ni toda descripción semántica de datos privados.
   No constituye una garantía universal de privacidad del contenido.
6. Releer conversación admitida, ledger y todas las fuentes seleccionadas, incluida
   la principal y agendas, antes de enviar. Se comparan las citas y el digest de
   sus registros contenedores (incluyendo revisión/estado), no el paquete entero.
   Un registro citado cambiado/retirado invalida la pregunta con razón visible;
   cambios en fuentes no citadas u otros items no borran el borrador válido.
   Hilos humanos nuevos y agendas reevalúan el horario: aplazar conserva el texto.
   Una revalidación que tarda 60 segundos o llega a quiet hours no entrega.
7. Publicar el texto validado a través del outbox y destino fijo. **No** se envía
   el aviso «draft ready». Los fences de epoch, estado, permisos y revisión se
   comprueban de nuevo en el outbox. Revocar y reautorizar no legitima un borrador
   anterior. Una operación admitida en vuelo no puede retirarse del transporte.

## Configuración por el operador

Ejemplo mínimo sin integraciones externas (solo se usa un ledger con hechos
abiertos reales; no se inventa un saludo si está vacío). El principal debe ser el
que el operador haya autorizado para sus superficies, no un nombre inferido:

```json
{
  "enabled": true,
  "allow_reads": true,
  "principal": "owner-principal",
  "primary": "ledger",
  "calendars": [],
  "sources": {}
}
```

```sh
hermes continuity checkin-config ./approved-checkin.json
hermes continuity checkin-status
hermes continuity setup --timezone UTC --allow-model --mode prepare
hermes continuity tick
# Después de revisar evidencia/calidad y autorizar el destino propio:
hermes continuity setup --delivery-target telegram:YOUR_EXACT_CHAT_ID --allow-delivery --mode active
hermes continuity tick
```

Estos son comandos de operación futura, **no ejecutados contra un perfil real**.
Cambiar modo/consentimiento invalida la generación previa; el siguiente tick
puede limitarse a descartar ese borrador y requerir otro tick para preparar.
`tick`, no `send-pending`, es la ruta de revalidación/entrega de check-ins.
No usar habilitación/reenabling para reintentar UNKNOWN.
No hay cooldown global ni máximo diario de envíos generales. Un aplazamiento de
horario conserva el borrador. Un tick posterior puede sustituir transaccionalmente
aviso/hash/epoch solo después de revalidar el artefacto y su autoridad actual,
sin token, lease ni intento diario reservado. Tras cambio de epoch se descarta
la autoridad anterior y se requiere nueva síntesis; no se adopta el borrador
anterior. No se reciclan sending, unknown, acknowledged ni failed.
La admisión final vuelve a aplicar quiet hours, pausa, autorización y revisión
con el reloj del efecto, conservando la excepción de review para avisos.
Comprueba el único intento diario de check-in ya reservado sin reservar otra vez.
Un rechazo después de reservar conserva el intento consumido, aunque no invoque
transporte. No se promete cancelar efectos ya admitidos en vuelo.

`sources` asigna roles semánticos a selectores de adaptadores existentes, por
`role`, `scope` y opcional `path`; nunca a argumentos elegidos por el modelo.
Roles: `conversations`, `ledger`, `daily_notes`, `calendar_personal`,
`calendar_work`, `calendar_school`, `meeting_summaries`, `completed_training`,
`email`, `memory`. `memory` es secundaria y no puede ser principal ni la única cita.
Ejemplo de selecciones (requiere mappings autorizados compatibles):

```json
{
  "daily_notes": [{"role":"notes","scope":"daily","path":"{date}.md"}],
  "calendar_work": [{"role":"agenda","scope":"work"}],
  "email": [{"role":"mail","scope":"relevant"}]
}
```

Si se selecciona `calendar_work`, incluirlo también en `calendars`; no se puede
leer una agenda y omitirla de la elegibilidad. `{date}` expande hoy y los seis días
anteriores en la zona elegida. Máximo 24 lecturas tras expansión y paquete completo
acotado; no se trunca silenciosamente para hacer pasar la validación. Un archivo
estático no se presenta como historial de siete días.

Agendas entregan `events` con `start`/`end` (epoch o fecha ISO aware, también
`dateTime` nativo) y `window.start`/`window.end`. All-day sin zona o sin cobertura
inequívoca bloquea: no se adivina una hora. Entrenamiento requiere `entries` de
sesiones reales con `completed: true`; presets/planeados no se convierten en
entrenamientos realizados y producen PARTIAL cuando se filtran.

Un helper que declara `coverage: PARTIAL` conserva esa cobertura aunque el
subproceso termine correctamente. Una primaria OK/PARTIAL permite síntesis solo
con hechos efectivamente leídos y citas verificadas; UNAVAILABLE sigue bloqueada.
Las agendas seleccionadas deben ser OK para elegir un horario. Las citas usan
exclusivamente nombres de fuente de primer nivel enumerados en el schema: una
ruta/referencia más detallada no se reescribe para hacerla pasar. No existen cuotas
diarias `model`/`checkin_model` ni plazas reservadas de síntesis. Los contadores
históricos se conservan pero ya no gatean inferencia. Cada ejecución comparte el
límite de turnos actual del host entre extracción, borradores y check-in. Una
propuesta inválida consume un turno, no un mensaje entregado. El límite
`{day_end}` es exclusivo: un helper basado en fechas locales inclusivas debe
recibir `{day_start}` como última fecha, no la medianoche del día siguiente.

## Diagnóstico de ejecuciones y bloqueo

`Worker.tick` crea un medidor compartido por extracción, borradores y check-in y
lee `agent.max_turns` mediante `load_config_readonly`/`resolve_turn_limit` del host
en cada ejecución, sin constante propia. `tick.inference` y `why.inference` muestran
ID, límite, intentos y estado persistidos. Un turno es una llamada intentada a
PluginLlm, también si proveedor/parseo/validación fallan; no equivale a tokens ni
al número físico de solicitudes de red de sus retries/fallbacks internos.
`available`/`limit_reached` describen el medidor, no un proceso todavía ejecutándose.
No hay loop nuevo para agotar/reiniciar el límite ni una cuota por día.

Al agotarse, los jobs quedan `blocked` con `inference_turn_limit_exhausted`,
incluidos retries pendientes y el backlog inicial fuera del lote. El vínculo
persistido entre intento y ejecución impide repetir el mismo trabajo agotado tras
un crash/restart. Tick/resume/medianoche no lo reencolan; se necesita una fuente o
revisión nueva explícita, no borrar SQLite o reciclar un envío UNKNOWN.
La misma razón aparece en `checkin-status` para la pregunta bloqueada: el contexto
adquirido sin cambios no habilita otra inferencia por crear otro objeto/worker.
Un contexto adquirido nuevo sí puede permitir reconsiderar la pregunta pendiente.
Los registros de ejecución, admisiones y bloqueos son locales y `forget` los purga.

`checkin_days.reason` también distingue `contextual_timing_deferred`,
`calendar_unavailable_or_no_eligible_window`, `primary_evidence_unavailable`,
`model_consent_unavailable`, `delivery_consent_unavailable`,
`authorization_generation_changed`, `checkin_synthesis_failed`,
`cited_ground_changed`, `context_revalidation_expired`,
`delivery_policy_or_notice_blocked` y `delivery_attempt_unconfirmed`.
Un borrador válido aplazado conserva su texto. Un cambio en el registro citado
requiere nueva síntesis; otro item o fuente no citada no provoca pérdida de texto.
Esto verifica citas/estado/autorización y horario, no resolución semántica arbitraria
de contradicciones ni cierre automático nuevo. Las fuentes externas siguen sin
atomicidad distribuida y los retries internos del host no son un hard cap monetario.

## Adaptadores nuevos para activar sin ventanas vencidas

En el archivo `--adapters`, una agenda rolling tiene esta forma (sustituir cuenta,
calendar y zona por valores explícitamente autorizados, sin tokens/credenciales):

```json
[{"type":"composio-day-read","role":"agenda","scope":"work","mapping":{
  "action":"GOOGLECALENDAR_FIND_EVENT","account":"APPROVED_ACCOUNT",
  "calendar_id":"APPROVED_CALENDAR","timezone":"UTC","max_results":20
}}]
```

Se puede repetir para personal/escuela con scopes distintos. Usa el CLI Composio
ya disponible; no instala nada, crea conexiones ni recibe credenciales nuevas.
La salida paginada/incompleta bloquea, no se asume que la primera página sea todo.

Para adquirir historial, resúmenes, correo o entrenamientos mediante un conector
que no declare el contrato host-read, existe `command-read`:

```json
{"type":"command-read","role":"history","scope":"approved-human-surfaces","mapping":{
  "executable":"/absolute/operator-reviewed-reader",
  "sha256":"SHA256_OF_THE_APPROVED_EXECUTABLE",
  "args":["--since","{week_start}","--until","{day_end}"],
  "timezone":"UTC","approval":"operator-reviewed-read-only","timeout":10,
  "schema":{"type":"object","additionalProperties":false,"required":["text"],
            "properties":{"text":{"type":"string"}}}
}}
```

Es una **plantilla de interfaz**, no un comando de integración inventado: el
helper real debe existir, estar auditado y devolver JSON del esquema. El padre
puede seleccionar una herramienta de lectura ya comprobada, fijar su executable,
argv y hash, y registrar el resultado por fuente. `{day_start}`, `{day_end}` y
`{week_start}` se expanden por código en cada llamada; no hay shell, stdin de modelo
ni argumentos elegidos por inferencia. El hash fija el ejecutable, no toda su
cadena de dependencias: revisar también el código/dependencias que ejecuta.
No configurar un shell, `hermes chat`, envío, agenda/escritura, ni un intérprete con
código no auditado como si fueran una lectura aislada. La atestación del operador
es una frontera de confianza, no un detector automático de efectos.

Para una fuente `conversations`, seleccionar role/history en checkin-config;
para `daily_notes`, el MarkdownAdapter real lee `{date}.md` siete veces. Una fuente
sin helper real compatible se mantiene UNAVAILABLE. No hay bypass de la autoridad
nativa ni un lector inseguro que convierta todos los `role=user` de state.db en
humanos: la prueba de captura automática sigue siendo una puerta independiente.

## Respuestas y continuidad

```sh
hermes continuity checkin-response PRINCIPAL:YYYY-MM-DD --event ADMITTED_HUMAN_EVENT --kind maintenance
hermes continuity checkin-response PRINCIPAL:YYYY-MM-DD --event ANOTHER_ADMITTED_EVENT --kind wellbeing --followups ./item-links.json
```

`item-links.json` es una lista de `{"item":"EXACT_ID","revision":0}`. La asociación
es un control **local del operador**, no una clasificación automática ni un tool
expuesto al modelo. Solo acepta un evento humano admitido, del mismo principal,
observado desde el `due` del check-in original y no en el futuro. Se permiten
respuestas tardías, incluso de otro día, sin una edad máxima automática: el
operador debe confirmar que **pertenecen al check-in original**, no asociar una
respuesta ajena solo porque supera esa frontera temporal. El vínculo conserva
el ID/día original y no cubre automáticamente la fecha de recepción ni otro
check-in. Un evento solo puede asociarse una vez; una colisión no puede moverlo
ni reclasificar una respuesta previa. Esto no prueba pertinencia semántica ni
captura humana automática.
Mantenimiento no marca el día contestado. Wellbeing sí lo hace, pero no cierra
obligaciones. Los followups enlazan items existentes abiertos por ID/revisión;
no crean otra agenda, no reabren resueltos y permanecen en el lifecycle existente.
Nuevos compromisos de una respuesta pasan por la extracción/admisión ya existente.
El silencio no cambia obligaciones ni se convierte en respuesta. La asociación
humana y el recibo de transporte se conservan separadamente, incluso en carrera.

## Límites que impiden afirmar paridad con un despliegue personal

- Conversaciones locales son únicamente los turnos admitidos al plugin. Sin un
  adaptador autorizado adicional no equivalen a revisar todo WhatsApp/Desktop:
  aparecen PARTIAL o UNAVAILABLE, nunca una cobertura global inventada.
- No hay adaptadores dedicados nuevos de Granola, Sparky, correo u OpenViking.
  Pueden usarse lecturas/exportaciones compatibles ya autorizadas, pero no se
  afirma que una integración nativa arbitraria sea compatible. Host-read exige
  `readOnlyHint` **y** `continuityScopedRead` exacto; MCP sin ese contrato se rechaza.
- `composio-day-read` añade una ventana rolling calculada en cada lectura según
  zona local, conservando acción/cuenta/calendar fijos; probado también al día
  siguiente. El adaptador legacy `composio-read` sigue usando ventanas fijas.
- `command-read` permite un helper de lectura auditado por el operador: ejecutable
  absoluto con SHA-256 fijado, argv fijo, esquema JSON cerrado, timeout y expansión
  local de ventanas. No es sandbox ni prueba de que código arbitrario sea read-only.
  No se afirma compatibilidad con un servicio hasta ejercitar su helper real.
- No existe runner CLI con herramientas ilimitadas. La CLI nativa admite query
  file/stream-json/skills/budgets/source, pero seleccionar skill o limitar turnos
  no constituye sandbox ni prueba de lecturas. Aquí se usa el mecanismo real
  equivalente de lecturas deterministas autorizadas + síntesis del paquete.
- Validación estructural/citas no demuestra comprensión semántica, ausencia
  universal de presión/diagnóstico ni calidad humana longitudinal. Instrucciones
  de seguridad y pruebas fixture no sustituyen evaluación de contenido real.
- Las fuentes externas no participan en una transacción distribuida: se releen
  justo antes del efecto y se exige frescura, pero un tercero puede cambiar
  después. No se promete atomicidad externa ni exactly-once del proveedor.
- No se escribe diario, memoria externa ni notas: eso requiere consentimiento y
  adaptador de escritura aparte, no implementado en este vertical.
- `checkin-status` muestra contenido privado derivado al operador local. `forget`
  purga también días/respuestas junto con el resto de datos derivados; fuentes,
  backups y logs de proveedor siguen fuera de ese borrado lógico.

## Corte sin duplicados (propuesta; no ejecutada)

Mantener planner y entrega anteriores mientras el nuevo workflow esté en prepare,
sin permiso de envío. Obtener paridad de fuentes, ventanas rolling, nuevo turno
humano admitido, respuesta, pregunta útil y recibo positivo verificable en un
canary separado; no reinterpretar UNKNOWN anterior. Evaluar varios días y causas
de no-entrega, no solo número de notificaciones. Antes del cambio, escoger un día
nuevo sin intento previo; inventariar pendientes del planner/one-shot y asignar
un único dueño de entrega. Solo en el corte autorizado retirar/fencear pendientes
del sender anterior y habilitar el nuevo, conservando el planner anterior hasta
aceptar el reemplazo completo. No dejar ambos senders activos. Un rollback
conserva ledger y presupuesto de intentos; si el nuevo ya intentó o quedó UNKNOWN,
el anterior no debe enviar otro check-in ese día. No migrar datos privados al
repositorio/distribución. El padre decide y ejecuta el corte.
