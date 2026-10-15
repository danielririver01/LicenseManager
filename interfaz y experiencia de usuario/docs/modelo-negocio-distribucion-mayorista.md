# FRUBBER POS — Modelo de Negocio y Distribución Mayorista (Opción B)
**Versión:** 2.1  
**Fecha:** 15 de octubre de 2026  
**Estructura:** Titular Principal con capacidad de dar de alta al Co-Titular (Socio) y Administradores  
**Estado:** Aprobado para Distribución y Técnicos de Balanzas  
**Ubicación en Google Drive:** Carpeta `License Manager` (`04 - Modelo de Negocio y Distribución Mayorista`)

---

## 1. Resumen Ejecutivo y Estructura de Seguridad

Frubber POS es un software de punto de venta y pesaje diseñado específicamente para verdulerías, fruterías y minimercados, caracterizado por su funcionamiento **100% offline**, alta velocidad operativa y vinculación directa con balanzas electrónicas mediante puerto serial (COM / USB).

### Gobernanza y Creación de Usuarios desde el Panel (UI-09)
Para garantizar la máxima seguridad y control del negocio:
* **Titular Principal (Owner Primario):** Eres tú, quien realiza la configuración inicial de License Manager y custodia la llave privada maestra ECDSA P-256 (sellada con DPAPI en el servidor local).
* **Creación Segura del Co-Titular (Socio) en UI-09:** Desde tu panel de administración tienes habilitada la opción de seleccionar el rol al crear un usuario:
  1. **Co-Titular / Socio:** Al crearlo, le otorgas acceso total a License Manager (supervisión de emisiones, historial de auditoría inmutable, visualización de recaudación en COP y módulo de liquidación de utilidades UI-11).
  2. **Administrador Delegado:** Para operadores o asistentes de campo que solo emiten licencias y registran clientes, pero **no** pueden administrar usuarios ni ver liquidaciones societarias (error 403 estricto).

---

## 2. Modelo de Distribuidor / Mayorista Prepago (Opción B)

Para lograr una penetración ágil sin los costos fijos ni la fricción de un equipo de ventas propio, la sociedad se apalanca en los **técnicos de balanzas, instaladores de cableado comercial y proveedores de hardware POS**:
1. **Flujo de caja anticipado:** Los socios reciben el pago del paquete por adelantado antes de que el técnico instale las licencias.
2. **Cero fricción en campo:** El técnico resuelve la conexión física del puerto COM, la calibración de la balanza, la impresora térmica de 58/80 mm y la capacitación en el mostrador.
3. **Incentivo económico alineado:** El técnico gana un margen sustancial por la reventa de la licencia más el 100% de sus honorarios de instalación y servicios de valor agregado.
4. **Control criptográfico centralizado:** Solo License Manager de los socios firma y genera licencias válidas `FRB1` bajo ECDSA P-256.

---

## 3. Estructura de Paquetes Mayoristas para Técnicos y Distribuidores

El precio de venta oficial sugerido al público final (dueño de la verdulería) es de **$ 450.000 COP** por equipo/caja (licencia vitalicia).

La Sociedad comercializa los cupos prepagados a los técnicos aliados bajo la siguiente escala:

| Paquete Mayorista | Cupo de Licencias | Inversión del Técnico (COP) | Costo Unitario / Licencia | PVP Oficial al Cliente | Ganancia Neta Técnico (COP) | Margen Comercial |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Paquete Inicial** | 3 licencias | $ 960.000 | $ 320.000 | $ 450.000 | **$ 130.000 / lic** | **28.9%** |
| **Paquete Profesional** | 5 licencias | $ 1.450.000 | $ 290.000 | $ 450.000 | **$ 160.000 / lic** | **35.5%** |
| **Paquete Máster** | 10 licencias | $ 2.600.000 | $ 260.000 | $ 450.000 | **$ 190.000 / lic** | **42.2%** |

---

## 4. Fuentes de Ganancia Adicional para el Técnico (El Gancho Comercial)

El modelo está concebido para que el técnico no solo vea a Frubber como un software, sino como **su mejor herramienta de negocio integral**:

1. **Margen directo por software:** Gana entre $ 130.000 y $ 190.000 COP netos por cada licencia que activa.
2. **Servicio de Instalación y Calibración Física:** El técnico factura entre **$ 50.000 y $ 80.000 COP** adicionales por conectar la balanza, probar la trama de peso continuo, configurar la impresora térmica y calibrar el cajón monedero.
3. **Carga de Catálogo y Capacitación:** El técnico factura entre **$ 50.000 y $ 100.000 COP** por cargar el inventario inicial de frutas/verduras y enseñar a los cajeros.
4. **Venta o Adecuación de Hardware:** El técnico arma combos completos para el verdulero (PC reacondicionado + Balanza + Gaveta + Frubber POS) con márgenes de hardware propios que superan los $ 300.000 - $ 500.000 COP.
5. **Mantenimiento Preventivo:** Fideliza a la verdulería para limpiezas de balanzas y soporte técnico físico recurrente.

> **Ingreso típico por cliente para el técnico:** Entre **$ 230.000 y $ 350.000 COP** por cada verdulería donde implementa la solución completa.

---

## 5. Flujo Operativo y Emisión mediante License Manager

```
[ Técnico ] ---> Compra Paquete Prepago (Transferencia a cuenta de la Sociedad)
      │
      ▼
[ Titular / Co-Titular ] -> Registran recarga de cupos en License Manager (UI-07 / UI-11)
      │
      ▼
[ Verdulería ] -> Técnico instala Frubber POS -> Genera Código Hardware (16 hex)
      │
      ▼
[ Emisión ] ----> Técnico envía los 16 hex al Titular o Administrador Delegado
      │
      ▼
[ Criptografía ]> License Manager valida, firma con ECDSA P-256 (DPAPI) y descuenta 1 cupo
      │
      ▼
[ Activación ] -> Técnico ingresa licencia FRB1 en la verdulería -> POS Activado 100% Offline
```

### Reglas de Emisión:
- **Validación estricta de 16 caracteres hexadecimales** (`[0-9A-Fa-f]{16}`).
- **Asignación contable:** El valor se registra como prepago consumido ($ COP) y queda vinculado a la verdulería y equipo en UI-04 y UI-06.
- **Sin conexión requerida en la verdulería:** Todo el proceso de activación ocurre de forma offline.

---

## 6. Política de Garantía y Reemplazos de Hardware

1. **Licencia Vitalicia e Intransferible:** Cada licencia `FRB1` está firmada matemáticamente para el procesador y placa base del equipo específico. No se puede copiar a otra máquina.
2. **Reemplazo por Daño Físico Comprobado:**
   - Si la máquina sufre daño irreparable de disco, placa base o robo dentro del **primer año**, se reemite una licencia para el nuevo equipo bajo categoría **"Reemplazo" con costo $ 0 COP** de software.
   - El técnico debe certificar el cambio de hardware y el código viejo queda marcado como *Revocado por reemplazo* en el registro de License Manager.
   - El técnico es libre de cobrar sus honorarios de reinstalación física y configuración al cliente final.
3. **Ampliación de Cajas (Multi-puesto):**
   - Si la verdulería añade una segunda o tercera caja de cobro con balanza, cada caja adicional requiere su propia licencia individual (descontando cupo regular del técnico).

---

## 7. Mapeo de Roles y Permisos en el Dashboard

| Módulo en License Manager | Titular Principal | Co-Titular (Socio) | Administrador Delegado |
| :--- | :---: | :---: | :---: |
| **Crear Usuarios (UI-09)** | **SÍ (Total)** | **SÍ (Total)** | **BLOQUEADO (403)** |
| **Crear Co-Titulares** | **SÍ (Exclusivo Titulares)** | **SÍ** | **BLOQUEADO (403)** |
| **Liquidación y Utilidades (UI-11)** | **SÍ** | **SÍ** | **BLOQUEADO (403)** |
| **Auditoría Inmutable (UI-08)** | **SÍ** | **SÍ** | **Solo lectura básica** |
| **Emitir Licencias (UI-05)** | **SÍ** | **SÍ** | **SÍ** |
| **Registrar Verdulerías (UI-03)** | **SÍ** | **SÍ** | **SÍ** |
| **Registrar Cobros COP (UI-07)** | **SÍ** | **SÍ** | **SÍ** |
