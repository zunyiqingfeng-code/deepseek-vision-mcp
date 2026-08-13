/**
 * vision-send-image host half: registers an HTTP route that accepts a
 * base64 data-URL image, saves it under ~/.dsh/vision-uploads, and returns
 * the local path. The client half posts here from the composer.
 */
import { mkdir, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { join } from 'node:path'

export const name = 'vision-send-image'

export const inject = ['webServer']

export function apply(ctx) {
  const off = ctx.webServer.register({
    kind: 'exact',
    path: '/vision-upload',
    handler: async (req, res) => {
      let body = ''
      for await (const chunk of req) body += chunk
      let payload
      try {
        payload = JSON.parse(body)
      } catch {
        res.writeHead(400, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ ok: false, error: 'invalid JSON body' }))
        return
      }
      const dataUrl = String((payload && payload.dataUrl) || '')
      const m = /^data:(image\/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=]+)$/.exec(dataUrl)
      if (!m) {
        res.writeHead(400, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ ok: false, error: 'unsupported image format (PNG/JPEG/WebP/GIF only)' }))
        return
      }
      const mediaType = m[1]
      const b64 = m[2]
      if (b64.length > 24 * 1024 * 1024) {
        res.writeHead(413, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ ok: false, error: 'image too large (>18MB)' }))
        return
      }
      const ext = mediaType === 'image/jpeg' ? 'jpg' : mediaType.slice(6)
      try {
        const dir = join(homedir(), '.dsh', 'vision-uploads')
        await mkdir(dir, { recursive: true })
        const stamp = new Date().toISOString().replace(/[-:]/g, '').slice(0, 14)
        const name = `vision-${stamp}-${Math.floor(Math.random() * 100000)}.${ext}`
        const target = join(dir, name)
        await writeFile(target, Buffer.from(b64, 'base64'))
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ ok: true, path: target }))
      } catch (error) {
        res.writeHead(500, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ ok: false, error: String((error && error.message) || error) }))
      }
    },
  })
  ctx.effect(() => off)
}
