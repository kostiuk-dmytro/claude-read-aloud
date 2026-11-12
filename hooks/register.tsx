import type { Register } from 'claude-code'

// Read-aloud buttons for Claude's replies.
//
// Every paragraph of a reply gets a 🔊 button on its left: it reads from that
// paragraph to the end of the reply, back to back, and turns into ⏹ while that
// paragraph is heard. The first paragraph of a reply has a speed button on its
// right. When a reply finishes, its audio is made in the background, so a
// press plays at once. The speech work is done by scripts/read_aloud.py.

const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2]
const PYTHON_CANDIDATES = ['python3', 'python', 'py']

// Which paragraph is heard now (its button key), and the speed last set.
const playing = { plugin: 'claude-read-aloud', key: 'playing' } as const
const speedRef = { plugin: 'claude-read-aloud', key: 'speed' } as const

type Para = { text: string; speakable: boolean }
type Paths = { now: string; speed: string }

/** 1.25 -> "1.25×" */
const speedLabel = (s: number) => `${s}×`

/** Split markdown into paragraphs on blank lines, keeping code fences whole
 *  and gluing a lone heading onto the paragraph after it. Must match
 *  `paragraphs()` in scripts/read_aloud.py, or prefetched audio is missed. */
function paragraphs(md: string): Para[] {
  const chunks: string[] = []
  let cur: string[] = []
  let inFence = false
  for (const line of md.split('\n')) {
    if (/^\s*(```|~~~)/.test(line)) inFence = !inFence
    if (!inFence && line.trim() === '') {
      if (cur.length) chunks.push(cur.join('\n'))
      cur = []
    } else {
      cur.push(line)
    }
  }
  if (cur.length) chunks.push(cur.join('\n'))

  const out: Para[] = []
  for (let i = 0; i < chunks.length; i++) {
    let text = chunks[i] ?? ''
    const isHeading = /^(#{1,6}\s.*|\*\*[^*\n]+\*\*:?)$/.test(text.trim())
    const after = chunks[i + 1]
    if (isHeading && after !== undefined && !/^\s*(```|~~~)/.test(after)) {
      text = `${text}\n\n${after}`
      i++
    }
    const isCode = /^\s*(```|~~~)/.test(text) && /(```|~~~)\s*$/.test(text)
    const isTable = text.trim().startsWith('|')
    out.push({ text, speakable: !isCode && !isTable })
  }
  return out
}

const HELP = [
  'claude-read-aloud: press 🔊 beside a paragraph to hear the reply from there.',
  '',
  '/read-aloud stop          stop reading',
  '/read-aloud speed 1.25    playback speed, 0.5 to 2',
  '/read-aloud voice <name>  a Piper voice (en_US-ryan-medium) or an edge-tts voice (en-US-AvaNeural); "auto" picks by language',
  '/read-aloud auto on|off   read every reply aloud automatically',
  '/read-aloud doctor        check Python, Piper, edge-tts and FFmpeg',
].join('\n')

export const register: Register = on => {
  // Found at session start (and again after a reload): the Python that runs
  // the script, and the state files the buttons read.
  let python: string | undefined
  let paths: Paths | undefined
  // The poller following the background reader (one at a time).
  let tick: { cancel: () => void } | undefined

  const ready = () => Boolean(python && paths)
  const NOT_READY = [
    'Read aloud is not ready: no Python found that runs the plugin.',
    'Install Python 3.9+, then: pip install piper-tts edge-tts',
    'Install FFmpeg (ffplay) and put it on PATH, then restart Claude Code.',
    'If Python has another name, set CLAUDE_READ_ALOUD_PYTHON to its full path.',
  ].join('\n')

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'read-aloud',
      description: 'Read-aloud for replies: stop, speed, voice, auto, doctor',
      argumentHint: '[stop | speed <0.5-2> | voice <name|auto> | auto on|off | doctor]',
    })
    const script = `${$.plugin.root}/scripts/read_aloud.py`
    // Find a Python that runs the script: CLAUDE_READ_ALOUD_PYTHON if set,
    // else the usual names. In the background, so the session starts at once.
    void (async () => {
      const fromEnv = await $.env.get('CLAUDE_READ_ALOUD_PYTHON')
      for (const candidate of fromEnv ? [fromEnv] : PYTHON_CANDIDATES) {
        try {
          const r = await $.process.run([candidate, script, '--paths'], { timeoutMs: 8000 })
          if (r.exitCode === 0 && r.stdout.trim().startsWith('{')) {
            paths = JSON.parse(r.stdout) as Paths
            python = candidate
            return
          }
        } catch {
          // not this one
        }
      }
    })()
    return next(e)
  })

  // When a reply is finished, make its audio in the background (and read it
  // aloud if auto-read is on). The script returns at once.
  on('classic.Stop', async ($, e, next) => {
    if (python) {
      try {
        await $.process.run([python, `${$.plugin.root}/scripts/read_aloud.py`], {
          stdin: JSON.stringify(e),
          timeoutMs: 8000,
        })
      } catch {
        // prefetch is a speed-up only
      }
    }
    return next(e)
  })

  on('command.run', { command: 'read-aloud' }, async ($, e) => {
    if (!python) return { text: NOT_READY }
    const script = `${$.plugin.root}/scripts/read_aloud.py`
    const [sub = '', ...rest] = e.args.trim().split(/\s+/)
    const arg = rest.join(' ')
    const run = async (args: string[]) => {
      try {
        const r = await $.process.run([python ?? 'python', script, ...args], { timeoutMs: 30000 })
        return (r.stdout || r.stderr).trim()
      } catch (err) {
        return `Failed: ${String(err)}`
      }
    }
    switch (sub.toLowerCase()) {
      case 'stop':
        tick?.cancel()
        tick = undefined
        await $.state.set(playing, null)
        return { text: await run(['--stop']) }
      case 'speed': {
        const out = await run(arg ? ['--speed', arg] : ['--speed'])
        const n = Number(out)
        if (!Number.isFinite(n)) return { text: out }
        await $.state.set(speedRef, n)
        return { text: `Playback speed: ${n}×` }
      }
      case 'voice':
        return { text: await run(['--voice', arg || 'auto']) }
      case 'auto':
        return { text: `Auto-read: ${await run(arg ? ['--auto', arg] : ['--auto'])}` }
      case 'doctor': {
        const out = await run(['--doctor'])
        try {
          const report = JSON.parse(out) as Record<string, unknown>
          const lines = Object.entries(report).map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
          return { text: [`Python command: ${python}`, ...lines].join('\n') }
        } catch {
          return { text: out }
        }
      }
      default:
        return { text: HELP }
    }
  })

  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    if (e.props.isSummary || !e.props.text.trim()) {
      return next(e)
    }

    const script = `${$.plugin.root}/scripts/read_aloud.py`

    const { Box, Button, Markdown } = $.ui.resolve(e)
    const { value: now = null } = await $.state.get(playing)
    const paras = paragraphs(e.props.text)
    const showSpeed = e.props.isFirstOfReply

    let speed = 1
    if (showSpeed) {
      const { value } = await $.state.get(speedRef)
      if (typeof value === 'number') {
        speed = value
      } else if (paths) {
        try {
          const text = await $.fs.read(paths.speed)
          const n = typeof text === 'string' ? parseFloat(text) : NaN
          if (Number.isFinite(n)) speed = n
        } catch {
          // no file yet: 1×
        }
      }
    }

    const stop = async () => {
      tick?.cancel()
      tick = undefined
      await $.state.set(playing, null)
      if (!ready() || !python) return
      try {
        await $.process.run([python, script, '--stop'], { timeoutMs: 8000 })
      } catch {
        // nothing was playing
      }
    }

    const startFrom = async (start: number) => {
      if (!ready() || !python || !paths) {
        $.ui.toast('Read aloud is not ready: run /read-aloud for setup help')
        return
      }
      const nowFile = paths.now
      const key = `${e.requestId}-${start}`
      // Every speakable paragraph of the block (the voice is picked from all
      // of them, as the prefetch did), and where in that list to start.
      const all = paras
        .map((p, i) => ({ key: `${e.requestId}-${i}`, text: p.text, i, speakable: p.speakable }))
        .filter(it => it.speakable)
      const items = all.map(({ key, text }) => ({ key, text }))
      const from = Math.max(0, all.findIndex(it => it.i >= start))
      await $.state.set(playing, key)
      try {
        await $.process.run([python, script, '--say-from'], {
          stdin: JSON.stringify({ items, start: from }),
          timeoutMs: 8000,
        })
      } catch {
        $.ui.toast('Read aloud: could not start')
        await $.state.set(playing, null)
        return
      }
      // The script reads in the background; follow which paragraph is heard.
      tick?.cancel()
      let misses = 0
      tick = $.clock.every(500, async () => {
        let cur: string | null = null
        try {
          const text = await $.fs.read(nowFile)
          cur = typeof text === 'string' ? text.trim() || null : null
        } catch {
          cur = null
        }
        // Allow time for the first paragraph to be made (a voice's first use
        // downloads it, ~10 s or more on a slow connection).
        if (cur === null && ++misses < 60) return
        if (cur !== null) misses = 0
        const { value } = await $.state.get(playing)
        if (value !== cur) await $.state.set(playing, cur)
        if (cur === null) {
          tick?.cancel()
          tick = undefined
        }
      })
    }

    const sayFrom = (start: number) => async () => {
      if ((await $.state.get(playing)).value === `${e.requestId}-${start}`) {
        return stop()
      }
      return startFrom(start)
    }

    // Next speed; if this block is being read, go on at the new speed from
    // the paragraph being heard.
    const cycleSpeed = async () => {
      if (!ready() || !python) {
        $.ui.toast('Read aloud is not ready: run /read-aloud for setup help')
        return
      }
      const at = SPEEDS.findIndex(s => Math.abs(s - speed) < 0.01)
      const nextSpeed = SPEEDS[(at + 1) % SPEEDS.length] ?? 1
      try {
        await $.process.run([python, script, '--speed', String(nextSpeed)], { timeoutMs: 8000 })
      } catch {
        $.ui.toast('Read aloud: could not change the speed')
        return
      }
      await $.state.set(speedRef, nextSpeed)
      $.ui.toast(`Read aloud: speed ${speedLabel(nextSpeed)}`)
      const cur = (await $.state.get(playing)).value
      const prefix = `${e.requestId}-`
      if (cur && cur.startsWith(prefix)) {
        const index = Number(cur.slice(prefix.length))
        if (Number.isInteger(index)) await startFrom(index)
      }
    }

    const rows = paras.map((p, i) => {
      const key = `${e.requestId}-${i}`
      const isPlaying = now === key
      return (
        <Box key={key} flexDirection="row" gap={1} alignItems="flex-start">
          <Box width={4} flexShrink={0}>
            {p.speakable ? (
              <Button
                key={`btn-${key}`}
                label={isPlaying ? '⏹' : '🔊'}
                dimColor={!isPlaying}
                onPress={sayFrom(i)}
              />
            ) : (
              ''
            )}
          </Box>
          <Box flexGrow={1} flexShrink={1}>
            <Markdown text={p.text} />
          </Box>
          {showSpeed && i === 0 ? (
            <Box flexShrink={0}>
              <Button
                key={`speed-${e.requestId}`}
                label={speedLabel(speed)}
                dimColor
                onPress={cycleSpeed}
              />
            </Box>
          ) : (
            ''
          )}
        </Box>
      )
    })

    return (
      <Box flexDirection="column" gap={1}>
        {rows}
      </Box>
    )
  })
}
