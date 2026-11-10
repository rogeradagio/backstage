# Backstage — documentació tècnica

Envia a Magento (mòdul `Backstage_Bridge`) els fets de fulfilment d'Odoo com a esdeveniments idempotents.
Contracte: repositori `backstage-bridge`, `docs/contract/`. L'endpoint és `POST /rest/V1/backstage/events`.

## Esdeveniments

| Fet a Odoo | Esdeveniment | Correu |
|---|---|---|
| Botó de recollida a la comanda (albarà reservat i sense traspassos pendents) | `ready_for_pickup` | send |
| Lliurament al client validat (recollida) | `collected` | send |
| Lliurament al client validat (enviament) | `shipment_dispatched` | send |
| Factura publicada | `invoice_issued` | suppress |
| Rectificativa publicada | `credit_note_issued` | suppress |
| Comanda cancel·lada | `cancelled` | send |
| Comanda passa al subestat de preparació configurat (p. ex. enviada a un magatzem extern) | `preparing_at_warehouse` (fonts dels albarans oberts) | send |
| Devolució de client validada | `return_received` | suppress |
| Seguiment del transportista: `delivery_state = customer_delivered` (només domicili; desactivat per defecte) | `delivered` | send |
| Surt el primer tram d'un traspàs web | `in_transit_to_store` | suppress |

- **Outbox transaccional** (`backstage.outbox`): l'esdeveniment es crea a la mateixa transacció que el fet; `queue_job` l'envia al canal `root.backstage` (una execució simultània).
- **Respostes**: 2xx → `sent`; 4xx → `rejected` + activitat a la comanda; 5xx o error de xarxa → `failed` i reintent amb *backoff*.
- **`event_id` determinista**: `so-<id>-ready`, `picking-<id>-done`, `so-<id>-cancel`, `move-<id>-<empremta>` (una factura republicada corregida genera un esdeveniment nou; republicada igual, no).
- **Reprocés sense correus**: `with_context(backstage_suppress=True)`. Amb el **mode silenciós** del backend (`silent`), tots els esdeveniments van amb `suppress`.
- **Per revisar** (`REVIEW_DOMAIN` a `backstage_outbox.py`, repetit a l'acció `action_backstage_review`): `rejected`, o `failed` amb 5 intents o més. `problem` i `next_step` (calculats) expliquen el cas a l'usuari.
- **Descartar** (`discarded`): no s'envia mai; tanca l'activitat de la comanda i, si era un shipment, deixa de sumar-se a l'estoc de la font.
- **Activitats**: tipus propi `backstage.mail_activity_type_backstage`; la de rebuig es tanca sola quan l'esdeveniment s'envia o es descarta.
- **Configuració**: un únic registre `backstage.backend` (`backstage.backend_magento`): URL, token, dos interruptors independents: `enabled` (s'envia o no a Magento) i `silent` (mode silenciós: cap correu als clients; activat per defecte), store views (`backstage.store.view`) i fonts (`backstage.source`).
- **Prefixos**: `magento_*` per a dades que vénen de Magento (`magento_increment_id`, `magento_store_code`, `magento_shipping_method`, `magento_order_item_id`…); `backstage_*` per a l'estat intern del mòdul (`backstage_link_state`, `backstage_outbox_ids`, `backstage_stock_dirty`…).
- **Grups**: `backstage.group_backstage_user` i `backstage.group_backstage_manager` (privilegi `Backstage`).
- **Endpoints**: `POST /rest/V1/backstage/events`, `POST /rest/V1/inventory/source-items`, `GET /rest/V1/inventory/sources`, `GET /rest/V1/orders`, `GET /rest/V1/store/storeViews`.

Dependències: `mail`, `sale_stock`, `stock_delivery` (transportista, seguiment i línia de ports), `account` (core), `queue_job` (OCA/queue), `sale_substate` (OCA/sale-workflow; requereix `base_substate` d'OCA/server-ux).
`server_wide_modules` ha d'incloure `queue_job`.

## Traspassos web en 2 passos

- Per magatzem de botiga: Configuració → **Recollida a botiga** → **Configurar** (o pestanya **Recollida web** del magatzem). Crea `Transit <codi>` i el tipus de recepció web (`backstage_transit_location_id`, `backstage_receipt_type_id`).
- Qualsevol traspàs d'un altre magatzem cap a l'estoc d'aquesta botiga que serveixi una comanda web de recollida (per comanda, referència o `origin`) es redirigeix al trànsit en confirmar-se. La reposició normal no es toca.
- Quan surt: `in_transit_to_store` (suppress) i es crea la recepció.
- Cada línia de la recepció web queda **enllaçada amb el lliurament de la seva comanda** (`move_dest_ids`, el mateix mecanisme que les rutes de diversos passos). En validar-la, Odoo reserva aquelles unitats per a aquella comanda: una altra comanda de la botiga que esperi el mateix producte no se les pot endur.
- **Llest per recollir només amb el lliurament reservat del tot** (cada línia en `assigned`; l'albarà surt "Llest" encara que la reserva sigui parcial). Si ha arribat tot però el lliurament no està reservat del tot (recepcions d'abans de l'enllaç, reserves canviades a mà, línies que no venien amb el traspàs), no s'avisa el client i la comanda rep una activitat amb què falta i qui ho té reservat; un cop resolt, la botiga prem **Llest per recollir**.
- Quan la botiga valida la recepció completa: es reserva l'albarà de recollida i s'envia `ready_for_pickup` (send). Amb diferències: activitat a la comanda i cap avís al client.
- **Incidències de recepció** (`backstage.web.receipt.incident`): si en validar la recepció hi ha diferències amb el que falta per arribar del traspàs (menys, més, producte no esperat), s'obre un assistent on la botiga n'indica el motiu (no ha arribat, menys peces, més peces, no esperat, malmès, equivocat, altres) i un comentari; després continua la validació normal (backorder inclòs). Les diferències validades sense passar per l'assistent (escàner, processos automàtics) queden com a "Sense motiu".
- Les recepcions de backorder continuen sent recepcions web: es compara amb el que encara faltava i, quan arriba tot, s'avisa el client. Els faltants (no ha arribat, menys peces, sense motiu) es resolen sols; malmès, equivocat, més peces o no esperat queden oberts perquè el magatzem d'origen els resolgui (botó **Resoldre**, amb la resolució).

## Export d'estoc a MSI

```
font(p, L) = estoc a mà a L
           − reservat a L per a demanda que NO és de Magento
           + expedit des de L amb el shipment encara no confirmat a Magento
```

- "Demanda de Magento" = moviments d'una comanda web o que l'alimenten (`sale_line_id`, `picking.sale_id`, `reference_ids.sale_ids` o `origin` d'una comanda amb `magento_increment_id`).
- La mercaderia en trànsit web cap a una botiga se suma a la font d'aquella botiga (MSI ja la té reservada).
- S'activa globalment i **font a font** (`backstage.source.export_stock`): durant una migració, cada font l'exporta un sol sistema.
- Productes: emmagatzemables, amb referència interna i `backstage_stock_sync` (plantilla; es canvia des del diàleg **Estoc a Magento** del producte, que mostra `backstage.stock.compute_breakdown`).
- Canvis de reserva, moviments validats, cancel·lacions, quants i shipments confirmats marquen el producte (`backstage_stock_dirty`); el cron de 2 minuts envia aquests productes en lots (`POST /V1/inventory/source-items`, valors absoluts). Cron nocturn de reconciliació completa.
- Permisos de la integració: `Backstage_Bridge::events_write` (esdeveniments), `Magento_Sales::actions_view` (vincular comandes), `Magento_Backend::store` (store views), `Magento_InventoryApi::source` (fonts) i `Magento_InventoryApi::stock_source_item_assign` (export d'estoc). Cal `oauth/consumer/enable_integration_as_bearer = 1`.

## Vinculació amb la comanda de Magento

- D'un connector existent només es llegeix el número de comanda (`external_sales_order_ref`, si el camp existeix i la integració és `magento2`). La resta es demana a Magento (`GET /V1/orders?searchCriteria…increment_id`, `GET /V1/store/storeViews`).
- Es completa `magento_order_id`, `magento_store_code`, `magento_shipping_method` i, per línia, `magento_order_item_id` (l'ID del connector si coincideix; si no, per SKU, preferint el pare dels configurables).
- Quan: en confirmar la comanda (job), cada nit per a les pendents (cron) i amb el botó **Vincular amb Magento** (pestanya Magento de la comanda).
- Estats: `pending` → `linked` / `error` (activitat a la comanda: no trobada, número repetit entre botigues sense store view, línies sense correspondència).
- Si Magento no respon quan cal emetre: els esdeveniments de comanda es desen igualment i el `store_code` es completa en enviar-los; lliuraments i factures s'ajornen en un job que es reintenta fins que la comanda queda vinculada. Si la vinculació falla, l'esdeveniment queda `rejected` (`order_not_linked`) i es pot reintentar des de l'outbox un cop vinculada.

## Proves

```
odoo -d <bd> -u backstage --test-enable --test-tags /backstage --stop-after-init
```

## Traduccions

Els textos del codi són en anglès. Per generar el fitxer de traduccions des d'una base de dades amb el mòdul instal·lat:

```
odoo -d <bd> --i18n-export=backstage/i18n/backstage.pot --modules=backstage --stop-after-init
```

i a partir d'aquí `i18n/ca.po` i `i18n/es.po`.
