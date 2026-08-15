/**
 * Interface strings (FR-UI-20).
 *
 * v1 ships English only. Every user-facing string lives here rather than in a
 * component, so adding `ru.ts` and a locale switch is a data change — which is
 * the point: the structure has to exist before translation is cheap, and
 * retrofitting it later means touching every component.
 */

export const en = {
  nav: { record: 'Record', history: 'History', settings: 'Settings' },

  record: {
    idle: 'Ready to record',
    start: 'Record',
    startAria: 'Start recording',
    stop: 'Stop',
    pause: 'Pause',
    resume: 'Resume',
    mark: 'Mark',
    marked: 'Moment marked',
    preparing: 'Starting…',
    stopping: 'Finishing up…',
    recording: 'REC',
    paused: 'Paused',
    jumpToLive: 'Jump to live',
    batchNotice:
      'Batch mode transcribes after you stop. You will see the timer and level meter while recording, and the transcript when it is done.',
    emptyTranscript: 'Speak, and what you say will appear here.',
    emptyTranscriptBatch: 'Recording. The transcript appears after you press Stop.',
    unknownSpeaker: '· · ·',
    partialHint: 'still being recognised',
    translationPending: 'translating…',
    translationFailed: 'translation failed',
    level: 'Input level',
    elapsed: 'Elapsed',
    speakers: 'speakers',
  },

  preflight: {
    title: 'Before recording',
    checking: 'Checking the server and your microphone…',
    serverOk: 'Server reachable',
    serverFail: 'The server is not reachable.',
    serverFailRemedy: 'Start it, or check your network. You can record offline and upload later.',
    micOk: 'Microphone working',
    micSilent: 'The microphone is not picking anything up.',
    micSilentRemedy: 'Check that it is not muted and that the right device is selected.',
    micFail: 'No microphone access.',
    modelsLoading: 'The speech model is still loading — recording is unavailable.',
    modelsLoadingRemedy:
      'A first run downloads several gigabytes. Try again in a few minutes.',
    sourceOk: 'Capturing this machine’s audio',
    sourceSilent: 'This machine is not playing any sound.',
    sourceSilentRemedy: 'Start the video or call you want to record, then try again.',
    sourcePending: 'Choose what to share when the browser asks…',
    diskLow: 'Disk space is running low on the server.',
    diskFull: 'The server is too low on disk to start a recording.',
    recordOffline: 'Record offline',
    startNow: 'Start recording now',
    recordAnyway: 'Record anyway',
    cancel: 'Cancel',
  },

  connection: {
    connected: 'Connected',
    connecting: 'Connecting…',
    reconnecting: 'Reconnecting — still recording',
    offline: 'Offline — recording to this device',
    buffered: (count: number) => `${count} chunk${count === 1 ? '' : 's'} waiting to upload`,
    dropped: (ms: number) => `${(ms / 1000).toFixed(1)} s of audio was dropped`,
    resync: 'This view fell behind and has been refreshed.',
  },

  history: {
    title: 'History',
    searchPlaceholder: 'Search transcripts and summaries…',
    allTime: 'All time',
    allLanguages: 'All languages',
    tags: 'Tags',
    empty: 'No recordings yet.',
    emptyAction: 'Press Record to make your first one.',
    noResults: (query: string) => `Nothing matches “${query}”.`,
    noResultsAction: 'Clear the filters, or try a different phrase.',
    speakerCount: (n: number) => `${n} speaker${n === 1 ? '' : 's'}`,
    noArtifacts: 'no artifacts',
    live: 'Recording now',
    rename: (title: string) => `Rename “${title}”`,
    renameTitle: 'Rename recording',
    renamePlaceholder: 'Name this recording',
    delete: (title: string) => `Delete “${title}”`,
    deleteTitle: 'Delete this recording?',
    deleteDetail:
      'The transcript, audio, and any summaries go with it. This cannot be undone.',
    deleteLiveDetail:
      'This session is still recording. Deleting it stops the recording and discards everything captured so far. This cannot be undone.',
    deleteConfirm: 'Delete',
    untitled: 'Untitled session',
    showing: (shown: number, total: number) =>
      shown >= total ? `${total}` : `${shown} of ${total}`,
    loadMore: (remaining: number) =>
      `Load ${remaining} more recording${remaining === 1 ? '' : 's'}`,
  },

  session: {
    transcript: 'Transcript',
    summary: 'Summary',
    actions: 'Actions',
    export: 'Export',
    delete: 'Delete',
    edited: 'edited',
    editHint: 'Tap a line to correct it',
    renameSpeaker: 'Rename speaker',
    renameHint: 'Renaming applies to every line in this session.',
    stale: 'The transcript has been edited since these were generated.',
    rerun: 'Re-run',
    rerunning: 'Re-running…',
    copy: 'Copy',
    copied: 'Copied',
    share: 'Share',
    original: 'Original',
    translation: 'Translation',
    sideBySide: 'Side by side',
    noArtifacts: 'No artifacts yet.',
    noArtifactsAction: 'Enable a plugin in Settings, then re-run it here.',
    cloudNotice: (providers: string) => `Audio or text left this server via ${providers}.`,
    cost: (usd: number) => `$${usd.toFixed(4)} estimated on cloud services`,
    costEstimate: 'Estimated from published list prices and audio duration — not a bill.',
    costComponent: {
      asr: 'Speech recognition',
      translation: 'Translation',
      plugins: 'Plugins',
      llm: 'Language model',
    } as Record<string, string>,
    playbackUnavailable: 'Audio was not saved for this session.',
  },

  settings: {
    title: 'Settings',
    capture: 'Capture',
    backends: 'Backends',
    plugins: 'Plugins',
    server: 'Server',
    source: 'Capture from',
    sourceMicrophone: 'Microphone',
    sourceSystem: 'Audio playing on this machine',
    sourceBoth: 'Both, mixed',
    sourceHelp:
      'Capturing this machine’s audio takes a call before it becomes sound in a room — no loudspeaker, no room, no microphone. It is the biggest accuracy gain available for online meetings. Pick “Both” when you are in the call yourself.',
    sourceUnsupported:
      'This browser cannot capture audio playing on this machine. Chrome or Edge on a desktop can.',
    sourceConsent:
      'Everyone in a call is being recorded, including people who cannot see this screen. Whether you may do that is your responsibility, not the software’s.',
    device: 'Microphone',
    deviceHelp:
      'When recording a room, a USB microphone plugged into this device is the single biggest accuracy gain available.',
    languages: 'Languages',
    targetLanguage: 'Translate into',
    mode: 'Latency mode',
    audioProcessing: 'Browser audio processing',
    echoCancellation: 'Echo cancellation',
    noiseSuppression: 'Noise suppression',
    autoGainControl: 'Automatic gain control',
    audioProcessingHelp:
      'These are tuned for one voice on a call. For a meeting with several people, leave them off — noise suppression will quieten the people furthest from the microphone.',
    localOnly: 'Local only',
    localOnlyHelp: 'Blocks every outbound network call. Cloud-dependent features report as unavailable.',
    costCeiling: 'Cloud spend ceiling per session',
    ceilingReached: 'Cost ceiling reached — this session switched to local processing.',
    ceilingReachedNoLocal:
      'Cost ceiling reached, but no local backend is available, so cloud processing continues.',
    costCeilingHelp: 'On reaching it, the session continues locally instead of failing. 0 means no ceiling.',
    nextSession: 'Applies to the next session',
    restartRequired: 'Needs a server restart',
    pluginTrust:
      'Plugins run in-process with full server privileges. Installing one is equivalent to running arbitrary code on the server.',
    noPlugins: 'No plugins are installed.',
    noPluginsAction: 'Drop a .py file into the plugins directory and restart the server.',
    unreachable: 'Cannot reach the server.',
    unreachableRemedy:
      'Check that it is running and that this device can reach it. This page keeps working; it just has nothing to show.',
    modelsLoading: 'The speech model is still loading.',
    modelsLoadingRemedy:
      'On a first run this downloads several gigabytes. Recording is unavailable until it finishes — progress is in the server log.',
    modelsFailed: 'The speech model could not be loaded.',
    models: 'Speech models',
    defaultModel: 'Default model',
    defaultModelHelp:
      'Used for every recording that is not pinned to a single language. A change applies to the next session; a recording in progress keeps the model it started with.',
    perLanguage: 'Per language',
    perLanguageHelp:
      'No model is best at every language. A recording pinned to exactly one language uses the model chosen here; one that offers several uses the default, because recognition detects a single language per window and there is nothing to route on.',
    useDefault: 'Use default',
    onlyCovers: 'only recognises',
    availableModels: 'Available models',
    availableModelsHelp:
      'Only models already on this server can be selected. Downloading one takes minutes and happens in the background — you can leave this page.',
    download: 'Download',
    downloading: 'Downloading…',
    onDisk: 'on disk',
    notDownloaded: 'not downloaded',
    noCredential: 'no credential',
    cloudReady: 'cloud',
    modelsDir: 'Models directory',
    theme: 'Theme',
    themeSystem: 'Match device',
    themeLight: 'Light',
    themeDark: 'Dark',
  },

  presets: {
    title: 'Presets',
    save: 'Save as preset',
    namePlaceholder: 'Name this setup',
    empty: 'No presets yet.',
    emptyAction: 'Save your usual setup to start recording in one tap.',
    lastUsed: 'Last used',
  },

  errors: {
    generic: 'Something went wrong.',
    wakeLockUnsupported:
      'This browser cannot keep the screen awake. Keep the screen on and this tab in front, or recording will stop.',
    meteredConnection: (mbPerHour: number) =>
      `You appear to be on a metered connection. This will use about ${mbPerHour} MB per hour.`,
    insecureContext:
      'Microphone access needs a secure connection. Open this page over HTTPS, or on the server itself via localhost.',
    captureStopped: 'Recording stopped unexpectedly. Everything up to this point has been saved.',
    offlinePending: (n: number) =>
      `${n} recording${n === 1 ? '' : 's'} made offline, waiting to upload.`,
    uploadNow: 'Upload now',
  },

  notifications: {
    ready: (title: string) => `“${title}” is ready`,
    readyBody: 'The transcript and any summaries have finished processing.',
    enable: 'Notify me when processing finishes',
  },

  common: {
    cancel: 'Cancel',
    save: 'Save',
    close: 'Close',
    retry: 'Retry',
    loading: 'Loading…',
    unknown: 'Unknown',
    minutes: (n: number) => `${n} min`,
    seconds: (n: number) => `${n} s`,
  },
} as const

export type Strings = typeof en
