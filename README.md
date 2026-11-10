# Backstage

Connecta Odoo amb una botiga Magento. Preparació, enviament, factura i gestions de stock i shipping arriben a la botiga online per mantenir l'stock actualitzat, mantenir informat al client i proporcionant traçabilitat i gestió d'excepcions.  

## Què fa

| Quan a Odoo… | …a Magento |
|---|---|
| La botiga marca la comanda com a **Llest per recollir** | El client rep l'avís que pot passar a recollir-la |
| Es valida el lliurament al client | La comanda queda enviada (amb transportista i número de seguiment) o recollida |
| Es publica una factura o un abonament | El document apareix a la comanda de Magento |
| Es cancel·la la comanda | La comanda es cancel·la a Magento i s'avisa el client |
| Es valida una devolució | Magento la registra |
| Es mou, es reserva o es rep estoc | L'estoc venible de cada font s'actualitza en pocs minuts |

Per a la recollida a botiga, Backstage també gestiona el traspàs des del magatzem fins a la botiga i les diferències que es detectin en rebre'l.

## Abans de començar

- Odoo 19 amb `stock_delivery` (core) i els mòduls `queue_job` i `sale_substate` (OCA). `queue_job` ha de ser a `server_wide_modules`.
- A Magento, el mòdul `Backstage_Bridge` instal·lat i una integració amb els permisos `Backstage_Bridge::events_write`, `Magento_Sales::actions_view`, `Magento_Backend::store`, `Magento_InventoryApi::source` i `Magento_InventoryApi::stock_source_item_assign`, i l'opció *Allow OAuth Access Tokens to be used as standalone Bearer tokens* activada. configurar l'URL de la botiga magento i el token d'accés de la integració.

## Posada en marxa

Tot es fa des de **Backstage → Configuració**. Cada pas indica com saber que ha anat bé.

1. **Connexió.** Introdueix l'URL de la botiga i el token, i prem **Provar connexió**.
   ✓ Veuràs les store views i les fonts de Magento. Si falla, el missatge diu si el problema és l'URL, el token o un permís.

2. **Fonts.** A la pestanya **Fonts**, assigna a cada "source" de Magento la ubicació/magatzem d'Odoo que li correspon. Backstage en proposa una si els noms coincideixen.
   ✓ Cap font sense ubicació. Les fonts que no envien a client, per la gestió d'stock de proveidors per exemple, desmarca-les a **Envia a client**.

3. **Recollida a botiga**. A la pestanya **Recollida a botiga**, prem **Configurar** a cada magatzem que sigui botiga (també es pot fer des del magatzem, pestanya **Recollida web**).
   ✓ El magatzem queda marcat com a configurat: té una ubicació de trànsit i una operació **Recepció web**.

4. **Prova sense correus.** Prem **Activar**. Backstage comença en **Mode silenciós**: envia tota la informació a Magento, però Magento no envia cap correu al client.
   Treballa normalment unes quantes comandes i revisa-les a **Backstage → Per revisar**.
   ✓ La llista es queda buida i les comandes es veuen bé a Magento.

5. **En marxa.** Prem **Avisar els clients**. A partir d'ara els clients reben els correus de Magento. **Mode silenciós** els torna a aturar, i **Desactivar** deixa d'enviar res a Magento. Els dos interruptors són independents i cada canvi demana confirmació.

6. **Estoc**. A la pestanya **Estoc**, activa **Exportar estoc**, i a la pestanya **Fonts** marca **Exporta estoc** font a font.
   Activa només les fonts que Magento no rebi ja d'un altre sistema: cada font l'ha d'actualitzar un únic sistema.
   ✓ El botó **Estoc a Magento** d'un producte mostra la quantitat enviada a cada font i com s'ha calculat.

## Idiomes de la interfície

Backstage està disponible en **català**, **castellà** i **anglès**. L'idioma segueix les preferències de cada usuari d'Odoo, inclosos menús, camps, ajudes, estats, assistents i missatges del mòdul.

1. Activa els idiomes que necessitis a la configuració d'Odoo.
2. Després de desplegar aquesta versió, actualitza el mòdul **Backstage** perquè Odoo carregui els catàlegs dels idiomes instal·lats i reinicia els processos d'Odoo per recarregar les traduccions dels missatges Python.
3. Cada usuari pot triar l'idioma a les seves preferències i tornar a carregar la pàgina.

Els catàlegs són a `i18n/ca.po`, `i18n/es.po` i `i18n/en.po`; `i18n/backstage.pot` conté la plantilla de textos originals en anglès. Les dades introduïdes pels usuaris, l'historial ja desat i els missatges retornats per Magento conserven el seu idioma original.

## El dia a dia

**Botiga**
- Quan una comanda de recollida té tota la mercaderia, a la comanda apareix el botó **Llest per recollir**. Si no apareix, encara falta alguna cosa per arribar o per reservar.
- En validar una **Recepció web**, si el que arriba no coincideix amb el que s'ha enviat, Odoo demana el motiu de cada diferència. El client no rep l'avís fins que la diferència es resol.

**Magatzem central**
- **Backstage → Incidències de recepció** recull les diferències declarades per les botigues. Els faltants que arriben més tard es tanquen sols; la resta s'han de resoldre indicant què s'ha fet.

**Responsable de la botiga online**
- **Backstage → Per revisar** és l'única llista que cal mirar. Si és buida, tot ha arribat a Magento.
- Cada element explica què ha passat i què cal fer. Un cop corregida la causa, prem **Reintentar**; si ja no cal enviar-lo, **Descartar**.
- Els errors de xarxa o de Magento caigut es reintenten sols i no cal fer res. Només apareixen a la llista si duren (5 intents o més).
- **Backstage → Comandes web** llista les comandes de Magento i la seva vinculació; **Esdeveniments** és l'historial complet.

## Si alguna cosa no quadra

| Situació | On mirar |
|---|---|
| El client no ha rebut un avís | Pestanya **Magento** de la comanda: cada enviament amb el seu estat i la resposta de Magento. |
| La comanda diu **No vinculada** | Odoo no ha trobat la comanda a Magento o alguna línia no hi correspon. El motiu surt a la mateixa pestanya; corregeix-lo i prem **Tornar a vincular**. |
| La web mostra un estoc que no esperaves | Botó **Estoc a Magento** del producte: estoc a la ubicació, reserves d'altres vendes i enviaments pendents de confirmar. |
| No apareix **Llest per recollir** | L'albarà de lliurament no està totalment reservat o hi ha un traspàs pendent cap a la botiga. |

## Permisos

- **Backstage / Usuari**: veu l'app i la informació de Magento a comandes i albarans, i pot reintentar o descartar enviaments.
- **Backstage / Administrador**: a més, accedeix a la configuració i resol incidències.
- Les botigues no necessiten cap grup: el botó **Llest per recollir** i l'assistent de diferències són a la comanda i a la recepció, i les incidències també són a **Inventari → Operacions**.

Els detalls tècnics (contracte amb Magento, càlcul de l'estoc, reintents) són a [docs/TECHNICAL.md](docs/TECHNICAL.md).
