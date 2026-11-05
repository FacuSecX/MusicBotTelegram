<h1 align="center">Telegram Music Bot</h1>

<p align="center">
<img alt="Telegram Music Bo" src="http://imgfz.com/i/gQdONA3.png" title="Telegram Music Bot">
</p>
<br>

<p align="center">
<a href="https://github.com/FacuSecX"><img title="Autor" src="https://img.shields.io/badge/Author-FacuSecX%20-blue?style=for-the-badge&logo=github"></a>
<a href=""><img title="Version" src="https://img.shields.io/badge/Version-1.0-red?style=for-the-badge&logo="></a>
</p>

Hola, ¿estás cansado de ser un pobre diablo y no tener un mango? Bueno, yo también. 

Con este simple bot pueden reproducir la música en segundo plano y con el teléfono bloqueado. También permite crear o armar playlists y reproducirlas sin límites. Así que, si te querés ahorrar unos buenos mangos en suscripciones como Spotify, YouTube o cualquiera de esos servicios basura, acá tenés una opción rápida y fácil.

<br>

<p align="center">
<img alt="Telegram Music Bo" src="http://imgfz.com/i/JM1ZND6.png" title="Telegram Music Bot">
</p>
<br>


## Cómo funciona

El bot busca las canciones directamente en YouTube y te envía el audio para escucharlo desde el reproductor integrado de Telegram.

1. Buscá una canción desde el menú, escribiendo el artista y el título en el chat o usando `/buscar artista canción`.
2. El bot te muestra hasta seis resultados.
3. Tocá el que quieras: la descarga y el envío comienzan directamente, sin confirmaciones adicionales.
4. Cuando llegue el audio, tocá **Play** y listo: podés seguir escuchando mientras usás otras aplicaciones o tenés el teléfono bloqueado.

También podés guardar canciones en favoritos, crear playlists y añadir o quitar temas desde los botones del bot.

Como las búsquedas se realizan en YouTube, no dependés de un catálogo propio del bot. La disponibilidad de cada canción depende de las restricciones del video original.

Aunque está pensado para música, también permite obtener el audio de otros videos públicos. En ese caso, recibís el audio, no el video completo.

## Migracion de Playlists a otro servidor

Si tenés playlists con muchas canciones y querés migrarlas a otro bot o servidor, agregué la opción **Backups** al menú principal. Esta función permite exportar e importar tus playlists y favoritos desde la nube, conservando el orden de las canciones.

El proveedor de almacenamiento es **MEGA**. Para usarlo, creá una cuenta en [mega.nz](https://mega.nz/). Durante la instalación, el instalador `.SH` te pedirá el correo y la contraseña de esa cuenta y los guardará en el archivo `.env` privado del bot.

Para crear un respaldo, entrá en **Backups → Exportar Backup**. El bot generará el archivo y lo subirá a MEGA. Desde **Descargar Backup** podrás acceder al respaldo y copiar su enlace.

Cuando tengas el bot instalado en el nuevo servidor, entrá en **Backups → Importar Backup** y pegá el enlace completo de MEGA. El bot reemplazará las playlists y los favoritos actuales por los del respaldo y reiniciará su servicio para cargar los cambios.

Así podés trasladar tu biblioteca sin volver a armar todas las listas canción por canción. El respaldo guarda tus playlists, favoritos y referencias a las canciones.
 



## Instalación y uso

El instalador está preparado para un **VPS con Debian 12 o Debian 13**. Configura las dependencias y deja el bot funcionando como un servicio que se inicia automáticamente con el servidor.

Necesitás:

- Un VPS con Debian 12/13 y acceso como administrador.
- Un bot creado desde **@BotFather** y su Token.
- El **ChatID** de tu chat privado con el bot.

Antes de instalarlo, abrí tu bot en Telegram y tocá **Iniciar** para que pueda enviarte mensajes.

Descargá el proyecto y ejecutá el instalador:

```bash
sudo apt update
sudo apt install -y git

git clone https://github.com/FacuSecX/MusicBotTelegram.git
cd MusicBotTelegram

sudo bash bot-install.sh
```

Durante la instalación te pedirá el nombre del servicio, el Token y el ChatID. También permite configurar una salida de red mediante WARP para las consultas de audio a YouTube.

El instalador verifica los datos del bot. Si son incorrectos, vuelve a pedirlos sin descartar lo que ya instaló. Las credenciales se guardan en un archivo `.env` separado del código.

Una vez terminado, entrá al chat del bot y escribí `/menu` o directamente el nombre de una canción. No hace falta mantener tu PC encendida: el bot funciona desde el VPS.

### Comandos principales

| Comando | Función |
|---|---|
| `/menu` | Muestra el menú principal. |
| `/ocultarmenu` | Oculta el menú. |
| `/buscar artista canción` | Busca canciones en YouTube. También podés escribir la búsqueda directamente en el chat. |
| `/crearplaylist Nombre` | Crea una playlist. |
| `/playlists` | Muestra tus playlists. |
| `/favorites` | Muestra tus favoritos. |
| `/detener` | Cierra la playlist desde el bot y limpia sus mensajes del chat. |
| `/limpiarchat` | Retira mensajes recientes y conserva tus favoritos y playlists. También acepta `/limpiar` y `/clearchat`. |
| `/help` | Muestra la ayuda. |

#

## 🤝 Autor

**Creado por** [FacuSecX](https://github.com/FacuSecX/) 

 Si el proyecto te resulta útil, agradecemos que nos dejes tu estrellita en el repo ⭐❤️
