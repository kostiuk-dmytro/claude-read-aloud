import type { Register } from 'claude-code'

// Read-aloud buttons for Claude's replies.
//
// Every paragraph of a reply gets a 🔊 button on its left: it reads from that
// paragraph to the end of the reply, back to back, and turns into ⏹ while that
// paragraph is heard. The first paragraph of a reply has a speed button on its
// right. When a reply finishes, its audio is made in the background, so a
// press plays at once. The speech work is done by scripts/read_aloud.py.
//
// Setup is automatic: the first press (or /read-aloud setup) installs the
// speech packages into a private Python environment. Only Python is needed.

const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2]
// Usual names, then the usual places on macOS, where an app started from the
// Finder does not see the shell's PATH.
const PYTHON_CANDIDATES = [
  'python3',
  'python',
  'py',
  '/opt/homebrew/bin/python3',
  '/usr/local/bin/python3',
  '/usr/bin/python3',
]
const NO_PYTHON =
  'Read aloud needs Python 3.9 or newer. Install it from python.org, then restart Claude Code.'
const SETUP_POLL_MS = 3000
const SETUP_MAX_POLLS = 300 // 15 minutes

// Which paragraph is heard now (its button key), and the speed last set.
const playing = { plugin: 'claude-read-aloud', key: 'playing' } as const
const speedRef = { plugin: 'claude-read-aloud', key: 'speed' } as const

type Para = { text: string; speakable: boolean }
type Report = {
  now: string
  speed: string
  ready: boolean
  python: string | null
  problem: string | null
}

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

function parseReport(stdout: string): Report | undefined {
  try {
    const r = JSON.parse(stdout) as Report
    return typeof r.now === 'string' ? r : undefined
  } catch {
    return undefined
  }
}

const HELP = [
  'claude-read-aloud: press 🔊 beside a paragraph to hear the reply from there.',
  '',
  '/read-aloud setup         one-time setup (also starts on the first 🔊 press)',
  '/read-aloud stop          stop reading',
  '/read-aloud speed 1.25    speaking speed, 0.5 to 2',
  '/read-aloud voice <name>  a Piper voice (en_US-ryan-medium) or an edge-tts voice (en-US-AvaNeural); "auto" picks by language',
  '/read-aloud auto on|off   read every reply aloud automatically',
  '/read-aloud doctor        show what was found and what is missing',
].join('\n')

export const register: Register = on => {
  // Found at session start (and again after a reload):
  //   bootstrap: a Python that runs the script (standard library only)
  //   report:    the script's --paths answer: state files, readiness, and
  //              `python`, the interpreter with the speech packages
  let bootstrap: string | undefined
  let report: Report | undefined
  // The pollers: one follows the paragraph being heard, one waits for setup.
  let tick: { cancel: () => void } | undefined
  let setupTick: { cancel: () => void } | undefined

  const runner = () => (report?.ready && report.python ? report.python : undefined)

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'read-aloud',
      description: 'Read-aloud for replies: setup, stop, speed, voice, auto, doctor',
      argumentHint: '[setup | stop | speed <0.5-2> | voice <name|auto> | auto on|off | doctor]',
    })
    const script = `${$.plugin.root}/scripts/read_aloud.py`
    // In the background, so the session starts at once.
    void (async () => {
      const fromEnv = await $.env.get('CLAUDE_READ_ALOUD_PYTHON')
      for (const candidate of fromEnv ? [fromEnv] : PYTHON_CANDIDATES) {
        try {
          const r = await $.process.run([candidate, script, '--paths'], { timeoutMs: 8000 })
          const parsed = r.exitCode === 0 ? parseReport(r.stdout.trim()) : undefined
          if (parsed) {
            bootstrap = candidate
            report = parsed
            // Start loading the voice server now, so the first press is quick.
            const python = runner()
            if (python) {
              await $.process.run([python, script, '--warm'], { timeoutMs: 8000 }).catch(() => undefined)
            }
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
    const python = runner()
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
    if (!bootstrap) return { text: NO_PYTHON }
    const script = `${$.plugin.root}/scripts/read_aloud.py`
    const [sub = '', ...rest] = e.args.trim().split(/\s+/)
    const arg = rest.join(' ')
    // Settings and checks run with the bootstrap Python; reading needs the runner.
    const run = async (args: string[], python = bootstrap ?? 'python') => {
      try {
        const r = await $.process.run([python, script, ...args], { timeoutMs: 30000 })
        return (r.stdout || r.stderr).trim()
      } catch (err) {
        return `Failed: ${String(err)}`
      }
    }
    const refresh = async () => {
      const parsed = parseReport(await run(['--paths']))
      if (parsed) report = parsed
    }
    switch (sub.toLowerCase()) {
      case 'setup': {
        const out = await run(['--setup'])
        setupTick?.cancel()
        let polls = 0
        setupTick = $.clock.every(SETUP_POLL_MS, async () => {
          await refresh()
          if (report?.ready || ++polls > SETUP_MAX_POLLS || /failed/i.test(report?.problem ?? '')) {
            setupTick?.cancel()
            setupTick = undefined
            $.ui.toast(report?.ready ? 'Read aloud is ready: press 🔊' : (report?.problem ?? 'Setup stopped'))
          }
        })
        return { text: out }
      }
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
        return { text: `Speaking speed: ${n}×` }
      }
      case 'voice':
        return { text: await run(['--voice', arg || 'auto']) }
      case 'auto':
        return { text: `Auto-read: ${await run(arg ? ['--auto', arg] : ['--auto'])}` }
      case 'doctor': {
        await refresh()
        const out = await run(['--doctor'], runner() ?? bootstrap)
        try {
          const found = JSON.parse(out) as Record<string, unknown>
          const lines = Object.entries(found).map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
          return { text: [`python command: ${bootstrap}`, ...lines].join('\n') }
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
      } else if (report) {
        try {
          const text = await $.fs.read(report.speed)
          const n = typeof text === 'string' ? parseFloat(text) : NaN
          if (Number.isFinite(n)) speed = n
        } catch {
          // no file yet: 1×
        }
      }
    }

    // Not set up yet: start the one-time setup and say what is happening;
    // a toast says when it is ready. True when reading can start now.
    const ensureReady = async (): Promise<boolean> => {
      if (runner()) return true
      if (!bootstrap) {
        $.ui.toast(NO_PYTHON)
        return false
      }
      let out = ''
      try {
        const r = await $.process.run([bootstrap, script, '--setup'], { timeoutMs: 8000 })
        out = r.stdout.trim()
      } catch {
        out = 'Read aloud: could not start the setup. Run /read-aloud doctor.'
      }
      $.ui.toast(out || 'Setting up read-aloud...')
      if (!setupTick) {
        let polls = 0
        const python = bootstrap
        setupTick = $.clock.every(SETUP_POLL_MS, async () => {
          try {
            const r = await $.process.run([python, script, '--paths'], { timeoutMs: 8000 })
            const parsed = parseReport(r.stdout.trim())
            if (parsed) report = parsed
          } catch {
            // try again next time
          }
          const failed = /failed/i.test(report?.problem ?? '')
          if (report?.ready || failed || ++polls > SETUP_MAX_POLLS) {
            setupTick?.cancel()
            setupTick = undefined
            $.ui.toast(report?.ready ? 'Read aloud is ready: press 🔊' : (report?.problem ?? 'Setup stopped'))
          }
        })
      }
      return false
    }

    const stop = async () => {
      tick?.cancel()
      tick = undefined
      await $.state.set(playing, null)
      const python = runner()
      if (!python) return
      try {
        await $.process.run([python, script, '--stop'], { timeoutMs: 8000 })
      } catch {
        // nothing was playing
      }
    }

    const startFrom = async (start: number) => {
      if (!(await ensureReady())) return
      const python = runner()
      if (!python || !report) return
      const nowFile = report.now
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
        $.ui.toast('Read aloud: could not start. Run /read-aloud doctor.')
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
      const python = runner() ?? bootstrap
      if (!python) {
        $.ui.toast(NO_PYTHON)
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
