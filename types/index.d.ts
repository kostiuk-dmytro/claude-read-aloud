declare module 'claude-code' {
  interface PluginState {
    'claude-read-aloud': { playing: string | null; speed: number }
  }
}

export {}
