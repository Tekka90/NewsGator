<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { api, getToken } from '$lib/api';
  import { currentUser } from '$lib/stores';
  import { t, tNow, fmt, LOCALES, setLocale } from '$lib/i18n';
  import StoryViewsSettings from '$lib/components/StoryViewsSettings.svelte';
  import type { Category, CategorySuggestion, MailAccount, ManagedUser, ReaderAccount } from '$lib/types';

  let language = $state('');
  let uiLanguage = $state('');
  let saved = $state(false);
  let categories = $state<Category[]>([]);
  let newCategory = $state('');
  let suggestions = $state<CategorySuggestion[]>([]);
  let users = $state<ManagedUser[]>([]);
  let newUsername = $state('');
  let newUserPassword = $state('');
  let newUserAdmin = $state(false);
  let userError = $state('');
  let sys = $state<Record<string, string | number>>({});
  let overridden = $state<string[]>([]);
  let envLocked = $state<string[]>([]);
  let sysSaved = $state(false);
  let llmTest = $state<{
    chat: boolean;
    embeddings: boolean;
    errors: string[];
    api_key_hint?: string;
  } | null>(null);
  let qdrantTest = $state<{ ok: boolean; errors: string[]; url: string | null; version?: { version?: string } } | null>(null);
  let readeckTest = $state<{ ok: boolean; errors: string[]; url: string | null; user?: string; roles?: string[] } | null>(null);
  let report = $state<{
    current: { tau_attach: number; tau_gray: number };
    labeled_pairs: number;
    decisions_logged: number;
    candidates: { tau: number; precision: number; recall: number; f1: number }[];
    suggested_tau_attach: number | null;
  } | null>(null);

  // Story RSS feed: per-user, tokenized URL the user pastes into a reader.
  // The token is fetched fresh from the backend on mount — localStorage may be
  // empty even with a valid session cookie (iOS standalone PWA mid-session).
  let feedToken = $state(getToken());
  let feedCategory = $state('');
  let feedUnread = $state(false);
  let feedCopied = $state(false);
  let feedCategories = $state<string[]>([]);

  // Newsletter inboxes (IMAP): per-user accounts polled for newsletters.
  // The password is write-only — re-enter it only to change it.
  let mailAccounts = $state<MailAccount[]>([]);
  let mailHost = $state('');
  let mailPort = $state(993);
  let mailUsername = $state('');
  let mailPassword = $state('');
  let mailFolder = $state('');
  let mailSsl = $state(true);
  let mailError = $state('');
  let mailAdding = $state(false);
  let mailTest = $state<Record<number, { ok: boolean; errors: string[]; folder: string | null }>>({});
  let mailPolling = $state<number | null>(null);
  // Immediate feedback after "Poll now": messages found, processing is backgrounded
  let mailPollInfo = $state<Record<number, string>>({});
  // Inline edit: id of the account being edited + draft values.
  // editPassword stays blank unless the user wants to REPLACE the stored one.
  let mailEditingId = $state<number | null>(null);
  let editHost = $state('');
  let editPort = $state(993);
  let editUsername = $state('');
  let editPassword = $state('');
  let editFolder = $state('');
  let editSsl = $state(true);
  let mailSaving = $state(false);

  // Third-party RSS Reader accounts (Google Reader API standard)
  let readerAccounts = $state<ReaderAccount[]>([]);
  let readerTitle = $state('');
  let readerBaseUrl = $state('https://www.inoreader.com');
  let readerUsername = $state('');
  let readerPassword = $state('');
  let readerError = $state('');
  let readerAdding = $state(false);
  let readerTest = $state<Record<number, { ok: boolean; items_accessible: number; error: string | null }>>({});
  let readerPolling = $state<number | null>(null);
  let readerPollInfo = $state<Record<number, string>>({});
  let readerEditingId = $state<number | null>(null);
  let editReaderTitle = $state('');
  let editReaderBaseUrl = $state('');
  let editReaderUsername = $state('');
  let editReaderPassword = $state('');
  let readerSaving = $state(false);
  let readerArticles = $state(200);
  let readerBackfill = $state<string>('');
  let editReaderBackfill = $state<string>('');

  async function loadReaderAccounts() {
    try {
      readerAccounts = await api.readerAccounts.list();
    } catch {
      readerAccounts = [];
    }
  }

  function startEditMail(a: MailAccount) {
    mailError = '';
    mailEditingId = a.id;
    editHost = a.host;
    editPort = a.port;
    editUsername = a.username;
    editPassword = '';
    editFolder = a.folder;
    editSsl = a.use_ssl;
  }

  async function saveMailAccount(e: SubmitEvent) {
    e.preventDefault();
    if (mailEditingId === null) return;
    mailError = '';
    mailSaving = true;
    try {
      await api.mailAccounts.update(mailEditingId, {
        host: editHost,
        port: editPort,
        username: editUsername,
        folder: editFolder,
        use_ssl: editSsl,
        // blank = keep the stored password (it is never readable back)
        ...(editPassword ? { password: editPassword } : {})
      });
      mailEditingId = null;
      mailAccounts = await api.mailAccounts.list();
    } catch (err) {
      mailError = err instanceof Error ? err.message : tNow('settings.saveAccountFailed');
    } finally {
      mailSaving = false;
    }
  }

  async function addMailAccount(e: SubmitEvent) {
    e.preventDefault();
    mailError = '';
    mailAdding = true;
    try {
      await api.mailAccounts.create({
        host: mailHost,
        port: mailPort,
        username: mailUsername,
        password: mailPassword,
        folder: mailFolder,
        use_ssl: mailSsl
      });
      mailHost = '';
      mailUsername = '';
      mailPassword = '';
      mailFolder = '';
      mailAccounts = await api.mailAccounts.list();
    } catch (err) {
      mailError = err instanceof Error ? err.message : tNow('settings.addAccountFailed');
    } finally {
      mailAdding = false;
    }
  }

  async function testMailAccount(a: MailAccount) {
    mailTest = { ...mailTest, [a.id]: await api.mailAccounts.test(a.id) };
  }

  async function pollMailAccount(a: MailAccount) {
    mailPolling = a.id;
    try {
      const r = await api.mailAccounts.poll(a.id);
      mailPollInfo = {
        ...mailPollInfo,
        [a.id]:
          r.found === 0
            ? tNow('settings.mailNoNew')
            : tNow('settings.mailFound', { count: r.found })
      };
    } catch (err) {
      mailError = err instanceof Error ? err.message : tNow('settings.pollFailed');
    } finally {
      mailPolling = null;
      mailAccounts = await api.mailAccounts.list();
    }
  }

  async function toggleMailAccount(a: MailAccount) {
    await api.mailAccounts.update(a.id, { is_enabled: !a.is_enabled });
    mailAccounts = await api.mailAccounts.list();
  }

  async function removeMailAccount(a: MailAccount) {
    if (!confirm(tNow('settings.confirmRemoveInbox', { account: `${a.username}@${a.host}` })))
      return;
    mailError = '';
    try {
      await api.mailAccounts.remove(a.id);
      mailAccounts = await api.mailAccounts.list();
    } catch (err) {
      mailError = err instanceof Error ? err.message : tNow('settings.removeAccountFailed');
    }
  }

  function fmtMailChecked(d: string | null) {
    return d ? $fmt.dateTime(d) : $t('settings.never');
  }

  async function addReaderAccount(e: SubmitEvent) {
    e.preventDefault();
    readerError = '';
    readerAdding = true;
    try {
      await api.readerAccounts.create({
        provider: 'greader',
        title: readerTitle.trim() || undefined,
        api_base_url: readerBaseUrl.trim(),
        username: readerUsername.trim(),
        password: readerPassword || undefined,
        is_enabled: true,
        initial_import_count: Number(readerArticles),
        backfill_days: readerBackfill === '' ? null : Number(readerBackfill)
      });
      readerTitle = '';
      readerPassword = '';
      await loadReaderAccounts();
    } catch (err) {
      readerError = err instanceof Error ? err.message : tNow('settings.addReaderFailed');
    } finally {
      readerAdding = false;
    }
  }

  function startEditReader(a: ReaderAccount) {
    readerError = '';
    readerEditingId = a.id;
    editReaderTitle = a.title ?? '';
    editReaderBaseUrl = a.api_base_url;
    editReaderUsername = a.username;
    editReaderPassword = '';
    editReaderBackfill = a.backfill_days === null ? '' : String(a.backfill_days);
  }

  async function saveReaderAccount(e: SubmitEvent) {
    e.preventDefault();
    if (readerEditingId === null) return;
    readerSaving = true;
    try {
      await api.readerAccounts.update(readerEditingId, {
        title: editReaderTitle.trim() || undefined,
        api_base_url: editReaderBaseUrl.trim(),
        username: editReaderUsername.trim(),
        backfill_days: editReaderBackfill === '' ? null : Number(editReaderBackfill),
        ...(editReaderPassword ? { password: editReaderPassword } : {})
      });
      readerEditingId = null;
      await loadReaderAccounts();
    } catch (err) {
      readerError = err instanceof Error ? err.message : tNow('settings.updateReaderFailed');
    } finally {
      readerSaving = false;
    }
  }

  async function toggleReaderAccount(a: ReaderAccount) {
    await api.readerAccounts.update(a.id, { is_enabled: !a.is_enabled });
    await loadReaderAccounts();
  }

  async function removeReaderAccount(a: ReaderAccount) {
    if (!confirm(tNow('settings.confirmDeleteReader', { name: a.title || a.username }))) return;
    readerError = '';
    try {
      await api.readerAccounts.remove(a.id);
      await loadReaderAccounts();
    } catch (err) {
      readerError = err instanceof Error ? err.message : tNow('settings.removeAccountFailed');
    }
  }

  async function testReaderAccount(a: ReaderAccount) {
    try {
      const res = await api.readerAccounts.test(a.id);
      readerTest = { ...readerTest, [a.id]: res };
    } catch (err) {
      readerTest = {
        ...readerTest,
        [a.id]: { ok: false, items_accessible: 0, error: err instanceof Error ? err.message : tNow('settings.testFailedShort') }
      };
    }
  }

  async function pollReaderAccount(a: ReaderAccount) {
    readerPolling = a.id;
    readerPollInfo = { ...readerPollInfo, [a.id]: '' };
    try {
      const res = await api.readerAccounts.poll(a.id);
      readerPollInfo = {
        ...readerPollInfo,
        [a.id]: tNow('settings.readerPollDone', { articles: res.new_articles, synced: res.read_synced })
      };
      await loadReaderAccounts();
    } catch (err) {
      readerPollInfo = {
        ...readerPollInfo,
        [a.id]: tNow('settings.readerPollFailed', { error: err instanceof Error ? err.message : String(err) })
      };
    } finally {
      readerPolling = null;
    }
  }

  const feedUrl = $derived.by(() => {
    if (typeof window === 'undefined' || !feedToken) return '';
    const params = new URLSearchParams({ token: feedToken });
    if (feedCategory) params.set('category', feedCategory);
    if (feedUnread) params.set('unread', '1');
    return `${window.location.origin}/api/feed.xml?${params}`;
  });

  async function copyFeedUrl() {
    try {
      await navigator.clipboard.writeText(feedUrl);
    } catch {
      // Clipboard API is secure-context-only — fall back for plain-HTTP LAN use
      const el = document.createElement('textarea');
      el.value = feedUrl;
      document.body.appendChild(el);
      el.select();
      document.execCommand('copy');
      el.remove();
    }
    feedCopied = true;
    setTimeout(() => (feedCopied = false), 2000);
  }

  // Message keys: group title/hint = settings.g.<id>.title|hint, field label = settings.f.<key>.
  type Field = { key: string; secret?: boolean };
  // Grouped so each external service stays together with its test button.
  const sysGroups: { id: string; hint?: boolean; fields: Field[] }[] = [
    {
      id: 'llm',
      hint: true,
      fields: [
        { key: 'llm_base_url' },
        { key: 'llm_model' },
        { key: 'llm_api_key', secret: true },
        { key: 'embed_base_url' },
        { key: 'embed_model' },
        { key: 'llm_context_tokens' },
        { key: 'llm_trace_enabled' },
        { key: 'llm_trace_max_chars' }
      ]
    },
    {
      id: 'vector',
      hint: true,
      fields: [
        { key: 'vector_backend' },
        { key: 'qdrant_url' },
        { key: 'qdrant_api_key', secret: true }
      ]
    },
    {
      id: 'readeck',
      hint: true,
      fields: [{ key: 'readeck_base_url' }, { key: 'readeck_token', secret: true }]
    },
    {
      id: 'sharing',
      hint: true,
      fields: [{ key: 'share_languages' }]
    },
    {
      id: 'chatbot',
      hint: true,
      fields: [{ key: 'chat_enabled' }, { key: 'chat_top_k' }, { key: 'chat_candidates' }]
    },
    {
      id: 'catsug',
      hint: true,
      fields: [
        { key: 'category_suggestions_enabled' },
        { key: 'category_suggestion_min_articles' },
        { key: 'category_suggestion_window_days' }
      ]
    },
    {
      id: 'clustering',
      hint: true,
      fields: [{ key: 'tau_attach' }, { key: 'tau_gray' }, { key: 'freeze_after_hours' }]
    },
    {
      id: 'ingestion',
      fields: [
        { key: 'retention_days' },
        { key: 'feed_disable_after_days' },
        { key: 'feed_backfill_days' },
        { key: 'reader_poll_minutes' },
        { key: 'summary_language' },
        { key: 'summary_min_sentences' },
        { key: 'summary_max_sentences' },
        { key: 'headline_min_words' },
        { key: 'headline_max_words' }
      ]
    }
  ];

  onMount(async () => {
    language = $currentUser?.summary_language ?? '';
    uiLanguage = $currentUser?.ui_language ?? '';
    mailAccounts = await api.mailAccounts.list();
    await loadReaderAccounts();
    // Token for the RSS URL: prefer a fresh one from the backend (works when
    // localStorage lost it), fall back to whatever is already stored.
    try {
      feedToken = (await api.sessionToken()).token;
    } catch {
      /* keep the localStorage value */
    }
    // Categories for the feed filter come from the stories list — the taxonomy
    // endpoint is admin-only.
    const stories = await api.stories.list();
    feedCategories = [...new Set(stories.map((s) => s.category))].sort();
    if ($currentUser?.is_admin) {
      categories = await api.categories.list();
      suggestions = await api.categories.suggestions();
      users = await api.users.list();
      const s = await api.settings.get();
      sys = s.values;
      original = { ...s.values };
      overridden = s.overridden;
      envLocked = s.env_locked;
    }
  });

  // Only send fields the user actually changed — otherwise saving would persist
  // (and DB-store) every env-provided value, including the LLM key.
  let original = $state<Record<string, string | number>>({});

  async function saveSystem() {
    const changed: Record<string, string | number> = {};
    for (const [k, v] of Object.entries(sys)) {
      if (envLocked.includes(k)) continue; // env-set keys are read-only here
      if (String(v) !== String(original[k] ?? '')) changed[k] = v;
    }
    const res = await api.settings.patch(changed);
    sys = res.values;
    original = { ...res.values };
    overridden = res.overridden;
    envLocked = res.env_locked;
    sysSaved = true;
    setTimeout(() => (sysSaved = false), 2000);
  }

  async function testLlm() {
    llmTest = null;
    llmTest = await api.settings.testLlm();
  }

  async function testQdrant() {
    qdrantTest = null;
    qdrantTest = await api.settings.testQdrant();
  }

  async function testReadeck() {
    readeckTest = null;
    readeckTest = await api.settings.testReadeck();
  }

  async function loadReport() {
    report = await api.settings.thresholdReport();
  }

  async function saveLanguage() {
    $currentUser = await api.patchMe({ summary_language: language });
    saved = true;
    setTimeout(() => (saved = false), 2000);
  }

  async function saveUiLanguage() {
    $currentUser = await api.patchMe({ ui_language: uiLanguage });
    await setLocale(uiLanguage);
  }

  async function logout() {
    await api.logout();
    $currentUser = null;
    await goto('/login');
  }

  async function addCategory(e: SubmitEvent) {
    e.preventDefault();
    await api.categories.create(newCategory);
    newCategory = '';
    categories = await api.categories.list();
  }

  async function removeCategory(c: Category) {
    if (!confirm(tNow('settings.confirmDeleteCategory', { name: c.name }))) return;
    await api.categories.remove(c.id);
    categories = await api.categories.list();
  }

  async function acceptSuggestion(s: CategorySuggestion) {
    await api.categories.acceptSuggestion(s);
    categories = await api.categories.list();
    suggestions = await api.categories.suggestions();
  }

  async function dismissSuggestion(s: CategorySuggestion) {
    await api.categories.dismissSuggestion(s);
    suggestions = await api.categories.suggestions();
  }

  async function addUser(e: SubmitEvent) {
    e.preventDefault();
    userError = '';
    try {
      await api.users.create({
        username: newUsername,
        password: newUserPassword,
        is_admin: newUserAdmin
      });
    } catch (err) {
      userError = err instanceof Error ? err.message : String(err);
      return;
    }
    newUsername = '';
    newUserPassword = '';
    newUserAdmin = false;
    users = await api.users.list();
  }

  async function resetPassword(u: ManagedUser) {
    const password = prompt(tNow('settings.promptNewPassword', { username: u.username }));
    if (!password) return;
    userError = '';
    try {
      await api.users.update(u.id, { password });
    } catch (err) {
      userError = err instanceof Error ? err.message : String(err);
    }
  }

  async function toggleAdmin(u: ManagedUser) {
    userError = '';
    try {
      await api.users.update(u.id, { is_admin: !u.is_admin });
    } catch (err) {
      userError = err instanceof Error ? err.message : String(err);
      return;
    }
    users = await api.users.list();
  }

  async function removeUser(u: ManagedUser) {
    if (!confirm(tNow('settings.confirmDeleteUser', { username: u.username }))) return;
    userError = '';
    try {
      await api.users.remove(u.id);
    } catch (err) {
      userError = err instanceof Error ? err.message : String(err);
      return;
    }
    users = await api.users.list();
  }
</script>

<svelte:head><title>{$t('settings.title')}</title></svelte:head>

<h1>{$t('settings.title')}</h1>

<div class="card">
  <h2>{$t('settings.preferences')}</h2>
  <label>
    {$t('settings.summaryLanguage')}
    <input bind:value={language} maxlength="8" placeholder="en" />
  </label>
  <button onclick={saveLanguage}>{$t('settings.save')}</button>
  {#if saved}<span class="ok">{$t('settings.saved')}</span>{/if}
  <label>
    {$t('settings.interfaceLanguage')}
    <select bind:value={uiLanguage} onchange={saveUiLanguage}>
      <option value="">{$t('settings.interfaceLanguageAuto')}</option>
      {#each LOCALES as l (l.code)}
        <option value={l.code}>{l.name}</option>
      {/each}
    </select>
  </label>
  <p class="hint">{$t('settings.interfaceLanguageHelp')}</p>
</div>

<StoryViewsSettings />

<div class="card">
  <h2>{$t('settings.session')}</h2>
  <p class="hint">{$t('settings.signedInAs')} <strong>{$currentUser?.username}</strong>.</p>
  <button onclick={logout}>{$t('settings.logout')}</button>
</div>

<div class="card">
  <h2>{$t('settings.rssTitle')}</h2>
  <p class="hint">
    {$t('settings.rssHint')}
  </p>
  <div class="feedopts">
    <label>
      {$t('settings.category')}
      <select bind:value={feedCategory}>
        <option value="">{$t('settings.allCategories')}</option>
        {#each feedCategories as c (c)}
          <option value={c}>{c}</option>
        {/each}
      </select>
    </label>
    <label class="check">
      <input type="checkbox" bind:checked={feedUnread} /> {$t('settings.unreadOnly')}
    </label>
  </div>
  <div class="feedurl">
    <input readonly value={feedUrl} onfocus={(e) => e.currentTarget.select()} />
    <button onclick={copyFeedUrl}>{feedCopied ? $t('settings.copied') : $t('settings.copy')}</button>
  </div>
</div>

<div class="card">
  <h2>{$t('settings.mailTitle')}</h2>
  <p class="hint">
    {$t('settings.mailHint')}
  </p>
  {#if mailError}<p class="bad">{mailError}</p>{/if}
  <form class="add" onsubmit={addMailAccount}>
    <input bind:value={mailHost} placeholder={$t('settings.imapHostPh')} required />
    <input
      bind:value={mailPort}
      type="number"
      min="1"
      max="65535"
      style="max-width: 6rem"
      title={$t('settings.port')}
    />
    <input bind:value={mailUsername} placeholder={$t('settings.mailUserPh')} required />
    <input
      bind:value={mailPassword}
      type="password"
      placeholder={$t('settings.mailPasswordPh')}
      required
    />
    <input bind:value={mailFolder} placeholder={$t('settings.folderPh')} required />
    <label class="inline">
      <input type="checkbox" bind:checked={mailSsl} /> SSL
    </label>
    <button type="submit" disabled={mailAdding}>{mailAdding ? $t('settings.adding') : $t('settings.addInbox')}</button>
  </form>
  {#each mailAccounts as a (a.id)}
    <div class="mailacct">
      {#if mailEditingId === a.id}
        <form class="add" onsubmit={saveMailAccount}>
          <input bind:value={editHost} placeholder={$t('settings.imapHostPh')} required />
          <input
            bind:value={editPort}
            type="number"
            min="1"
            max="65535"
            style="max-width: 6rem"
            title={$t('settings.port')}
          />
          <input bind:value={editUsername} placeholder={$t('settings.mailUserPh')} required />
          <input
            bind:value={editPassword}
            type="password"
            placeholder={$t('settings.newPasswordKeepPh')}
          />
          <input bind:value={editFolder} placeholder={$t('settings.folderPh')} required />
          <label class="inline">
            <input type="checkbox" bind:checked={editSsl} /> SSL
          </label>
          <button type="submit" disabled={mailSaving}>{mailSaving ? $t('settings.saving') : $t('settings.save')}</button>
          <button type="button" onclick={() => (mailEditingId = null)}>{$t('settings.cancel')}</button>
        </form>
      {:else}
        <div class="row">
          <strong>{a.username}@{a.host}:{a.port}</strong>
          <span class="ovr">{a.folder}</span>
          <span class="ovr" class:env={a.is_enabled}>{a.is_enabled ? $t('settings.enabled') : $t('settings.disabled')}</span>
          <span class="actions">
            <button class="linkbtn" onclick={() => startEditMail(a)}>{$t('settings.edit')}</button>
            <button class="linkbtn" onclick={() => testMailAccount(a)}>{$t('settings.test')}</button>
            <button
              class="linkbtn"
              onclick={() => pollMailAccount(a)}
              disabled={!a.is_enabled || mailPolling === a.id}
            >
              {mailPolling === a.id ? $t('settings.polling') : $t('settings.pollNow')}
            </button>
            <button class="linkbtn" onclick={() => toggleMailAccount(a)}>
              {a.is_enabled ? $t('settings.disable') : $t('settings.enable')}
            </button>
            <button class="linkbtn" onclick={() => removeMailAccount(a)}>{$t('settings.delete')}</button>
          </span>
        </div>
      {/if}
      <p class="hint">
        {$t('settings.mailLastChecked', { date: fmtMailChecked(a.last_checked_at), uid: a.last_uid })}
        {#if !a.use_ssl}· {$t('settings.noSsl')}{/if}
      </p>
      {#if a.last_error}<p class="bad">{$t('settings.lastError', { error: a.last_error })}</p>{/if}
      {#if mailPollInfo[a.id]}<p class="ok">{mailPollInfo[a.id]}</p>{/if}
      {#if mailTest[a.id]}
        <p class={mailTest[a.id].ok ? 'ok' : 'bad'}>
          {mailTest[a.id].ok
            ? $t('settings.mailConnOk', { folder: mailTest[a.id].folder ?? '' })
            : mailTest[a.id].errors.join(', ')}
        </p>
      {/if}
    </div>
  {:else}
    <p class="hint">{$t('settings.noInbox')}</p>
  {/each}
</div>

<div class="card">
  <h2>{$t('settings.readerTitle')}</h2>
  <p class="hint">
    {$t('settings.readerHint')}
  </p>
  {#if readerError}<p class="bad">{readerError}</p>{/if}
  <form class="add" onsubmit={addReaderAccount}>
    <input bind:value={readerTitle} placeholder={$t('settings.readerTitlePh')} />
    <input bind:value={readerBaseUrl} placeholder={$t('settings.readerUrlPh')} required />
    <input bind:value={readerUsername} placeholder={$t('settings.readerUserPh')} required />
    <input
      bind:value={readerPassword}
      type="password"
      placeholder={$t('settings.readerPasswordPh')}
      required
    />
    <label class="hint">
      {$t('settings.readerArticles')}
      <input bind:value={readerArticles} type="number" min="1" max="5000" />
    </label>
    <label class="hint">
      {$t('settings.importWindow')}
      <input bind:value={readerBackfill} type="number" min="0" max="3650" placeholder={$t('settings.serverDefault')} />
    </label>
    <button type="submit" disabled={readerAdding}>{readerAdding ? $t('settings.adding') : $t('settings.addAccount')}</button>
  </form>
  {#each readerAccounts as a (a.id)}
    <div class="mailacct">
      {#if readerEditingId === a.id}
        <form class="add" onsubmit={saveReaderAccount}>
          <input bind:value={editReaderTitle} placeholder={$t('settings.accountTitlePh')} />
          <input bind:value={editReaderBaseUrl} placeholder={$t('settings.apiUrlPh')} required />
          <input bind:value={editReaderUsername} placeholder={$t('settings.readerUserPh')} required />
          <input
            bind:value={editReaderPassword}
            type="password"
            placeholder={$t('settings.newTokenKeepPh')}
          />
          <label class="hint">
            {$t('settings.importWindow')}
            <input bind:value={editReaderBackfill} type="number" min="0" max="3650" placeholder={$t('settings.serverDefault')} />
          </label>
          <button type="submit" disabled={readerSaving}>{readerSaving ? $t('settings.saving') : $t('settings.save')}</button>
          <button type="button" onclick={() => (readerEditingId = null)}>{$t('settings.cancel')}</button>
        </form>
      {:else}
        <div class="row">
          <strong>{a.title || a.username}</strong>
          <span class="ovr">{a.api_base_url}</span>
          <span class="ovr" class:env={a.is_enabled}>{a.is_enabled ? $t('settings.enabled') : $t('settings.disabled')}</span>
          <span class="actions">
            <button class="linkbtn" onclick={() => startEditReader(a)}>{$t('settings.edit')}</button>
            <button class="linkbtn" onclick={() => testReaderAccount(a)}>{$t('settings.test')}</button>
            <button
              class="linkbtn"
              onclick={() => pollReaderAccount(a)}
              disabled={!a.is_enabled || readerPolling === a.id}
            >
              {readerPolling === a.id ? $t('settings.polling') : $t('settings.pollNow')}
            </button>
            <button class="linkbtn" onclick={() => toggleReaderAccount(a)}>
              {a.is_enabled ? $t('settings.disable') : $t('settings.enable')}
            </button>
            <button class="linkbtn" onclick={() => removeReaderAccount(a)}>{$t('settings.delete')}</button>
          </span>
        </div>
      {/if}
      <p class="hint">
        {$t('settings.readerLastChecked', { date: fmtMailChecked(a.last_checked_at), username: a.username })}
      </p>
      {#if a.last_error}<p class="bad">{$t('settings.lastError', { error: a.last_error })}</p>{/if}
      {#if readerPollInfo[a.id]}<p class="ok">{readerPollInfo[a.id]}</p>{/if}
      {#if readerTest[a.id]}
        <p class={readerTest[a.id].ok ? 'ok' : 'bad'}>
          {readerTest[a.id].ok
            ? $t('settings.readerConnOk', { count: readerTest[a.id].items_accessible })
            : $t('settings.readerTestFailed', { error: readerTest[a.id].error || $t('settings.unknownError') })}
        </p>
      {/if}
    </div>
  {:else}
    <p class="hint">{$t('settings.noReader')}</p>
  {/each}
</div>

{#if $currentUser?.is_admin}
  <div class="card">
    <h2>{$t('settings.categoriesAdmin')}</h2>
    <form class="add" onsubmit={addCategory}>
      <input bind:value={newCategory} placeholder={$t('settings.newCategory')} required />
      <button type="submit">{$t('settings.add')}</button>
    </form>
    <ul>
      {#each categories as c (c.id)}
        <li>
          {c.name}
          {#if c.name !== 'Uncategorized'}
            <button class="link" onclick={() => removeCategory(c)}>{$t('settings.deleteLower')}</button>
          {/if}
        </li>
      {/each}
    </ul>
    {#if suggestions.length}
      <h3>{$t('settings.proposedCategories')}</h3>
      <p class="hint">
        {$t('settings.proposedHint')}
      </p>
      <ul class="suggestions">
        {#each suggestions as s (s.normalized_text)}
          <li>
            <strong>{s.label}</strong> — {$t('settings.articlesCount', { count: s.article_count })}
            {#if s.example_titles.length}
              <span class="hint">{$t('settings.exampleTitles', { titles: s.example_titles.join('; ') })}</span>
            {/if}
            <div class="actions">
              <button onclick={() => acceptSuggestion(s)}>{$t('settings.addAsCategory')}</button>
              <button class="link" onclick={() => dismissSuggestion(s)}>{$t('settings.dismiss')}</button>
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </div>

  <div class="card">
    <h2>{$t('settings.usersAdmin')}</h2>
    <form class="add" onsubmit={addUser}>
      <input bind:value={newUsername} placeholder={$t('settings.username')} minlength="3" required />
      <input
        type="password"
        bind:value={newUserPassword}
        placeholder={$t('settings.password')}
        minlength="8"
        autocomplete="new-password"
        required
      />
      <label class="inline">
        <input type="checkbox" bind:checked={newUserAdmin} /> {$t('settings.admin')}
      </label>
      <button type="submit">{$t('settings.addUser')}</button>
    </form>
    {#if userError}<p class="bad">{userError}</p>{/if}
    <ul>
      {#each users as u (u.id)}
        <li>
          {u.username}
          {#if u.is_admin}<span class="ovr">{$t('settings.admin')}</span>{/if}
          {#if u.id === $currentUser?.id}<span class="hint">{$t('settings.you')}</span>{/if}
          <span class="actions">
            <button class="linkbtn" onclick={() => resetPassword(u)}>{$t('settings.resetPassword')}</button>
            <button class="linkbtn" onclick={() => toggleAdmin(u)}>
              {u.is_admin ? $t('settings.revokeAdmin') : $t('settings.makeAdmin')}
            </button>
            {#if u.id !== $currentUser?.id}
              <button class="link" onclick={() => removeUser(u)}>{$t('settings.deleteLower')}</button>
            {/if}
          </span>
        </li>
      {/each}
    </ul>
  </div>

  <div class="card">
    <h2>{$t('settings.systemAdmin')}</h2>
    {#each sysGroups as group (group.id)}
      <section class="group">
        <h3>{$t(`settings.g.${group.id}.title`)}</h3>
        {#if group.hint}<p class="hint">{$t(`settings.g.${group.id}.hint`)}</p>{/if}
        <div class="grid">
          {#each group.fields as f (f.key)}
            {@const locked = envLocked.includes(f.key)}
            <label title={locked ? $t('settings.envLockedTitle', { name: f.key.toUpperCase() }) : undefined}>
              {$t(`settings.f.${f.key}`)}
              {#if locked}
                <span class="ovr env">env</span>
              {:else if overridden.includes(f.key)}
                <span class="ovr">{$t('settings.overridden')}</span>
              {/if}
              <input
                type={f.secret ? 'password' : 'text'}
                bind:value={sys[f.key]}
                autocomplete="off"
                disabled={locked}
              />
            </label>
          {/each}
        </div>

        {#if group.id === 'llm'}
          <div class="row">
            <button class="linkbtn" onclick={testLlm}>{$t('settings.testConnection')}</button>
            {#if llmTest}
              <span class:ok={llmTest.chat && llmTest.embeddings} class:bad={!llmTest.chat || !llmTest.embeddings}>
                {$t('settings.llmChat')}: {llmTest.chat ? '✓' : '✗'} · {$t('settings.llmEmbeddings')}: {llmTest.embeddings ? '✓' : '✗'}
                {#if llmTest.api_key_hint}· {$t('settings.keyInUse', { hint: llmTest.api_key_hint })}{/if}
                {#each llmTest.errors as err}<br /><small>{err}</small>{/each}
              </span>
            {/if}
          </div>
        {:else if group.id === 'vector'}
          <div class="row">
            <button class="linkbtn" onclick={testQdrant} disabled={sys.vector_backend !== 'qdrant'}>
              {$t('settings.testQdrant')}
            </button>
            {#if qdrantTest}
              <span class:ok={qdrantTest.ok} class:bad={!qdrantTest.ok}>
                {qdrantTest.ok
                  ? qdrantTest.version?.version
                    ? $t('settings.reachableVersion', { version: qdrantTest.version.version })
                    : $t('settings.reachable')
                  : $t('settings.failed')}
                {#each qdrantTest.errors as err}<br /><small>{err}</small>{/each}
              </span>
            {/if}
          </div>
        {:else if group.id === 'readeck'}
          <div class="row">
            <button class="linkbtn" onclick={testReadeck} disabled={!sys.readeck_base_url || !sys.readeck_token}>
              {$t('settings.testReadeck')}
            </button>
            {#if readeckTest}
              <span class:ok={readeckTest.ok} class:bad={!readeckTest.ok}>
                {readeckTest.ok ? $t('settings.connectedAs', { user: readeckTest.user ?? '' }) : $t('settings.failed')}
                {#each readeckTest.errors as err}<br /><small>{err}</small>{/each}
              </span>
            {/if}
          </div>
        {/if}
      </section>
    {/each}

    <div class="row">
      <button onclick={saveSystem}>{$t('settings.saveSystem')}</button>
      {#if sysSaved}<span class="ok">{$t('settings.saved')}</span>{/if}
    </div>
  </div>

  <div class="card">
    <h2>{$t('settings.feedbackAdmin')}</h2>
    <p class="hint">
      {$t('settings.feedbackHint')}
    </p>
    <button onclick={loadReport}>{$t('settings.generateReport')}</button>
    {#if report}
      <p>
        {$t('settings.reportSummary', { decisions: report.decisions_logged, labeled: report.labeled_pairs, tau: report.current.tau_attach })}
        {#if report.suggested_tau_attach}
          · <strong>{$t('settings.suggestedTau', { tau: report.suggested_tau_attach })}</strong>
        {:else}
          · {$t('settings.notEnoughData')}
        {/if}
      </p>
      {#if report.candidates.length}
        <div class="tablewrap">
          <table>
            <thead><tr><th>τ</th><th>{$t('settings.precision')}</th><th>{$t('settings.recall')}</th><th>F1</th></tr></thead>
            <tbody>
              {#each report.candidates as c (c.tau)}
                <tr class:best={c.tau === report.suggested_tau_attach}>
                  <td>{c.tau}</td><td>{c.precision}</td><td>{c.recall}</td><td>{c.f1}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    {/if}
  </div>
{/if}

<style>
  h2 { margin-top: 0; font-size: 1.05rem; }
  .ok { color: var(--ok); margin-left: 0.5rem; }
  .group { border-top: 1px solid var(--table-border); padding-top: 0.6rem; margin-top: 0.9rem; }
  .group:first-of-type { border-top: none; padding-top: 0; margin-top: 0; }
  .group h3 { font-size: 0.95rem; margin: 0 0 0.15rem; }
  .linkbtn {
    background: none; border: 1px solid var(--border-strong); border-radius: 6px;
    padding: 0.2rem 0.6rem; cursor: pointer; color: var(--accent); font-size: 0.9em;
  }
  .linkbtn:disabled { color: var(--disabled-text); border-color: var(--disabled-bg); cursor: not-allowed; }
  .add { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .add input { flex: 1 1 9rem; min-width: 0; }
  .feedopts { display: flex; gap: 1rem; align-items: flex-end; flex-wrap: wrap; margin-bottom: 0.6rem; }
  .feedopts label { display: flex; flex-direction: column; gap: 0.2rem; font-size: 0.9em; }
  .feedopts .check { flex-direction: row; align-items: center; gap: 0.35rem; padding-bottom: 0.35rem; }
  .feedurl { display: flex; gap: 0.5rem; }
  .feedurl input { flex: 1 1 0; min-width: 0; font-family: monospace; font-size: 0.85em; }
  .link {
    background: none;
    border: none;
    color: var(--error);
    text-decoration: underline;
    padding: 0;
  }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0 1.5rem;
  }
  .grid input { width: 100%; }
  @media (max-width: 700px) {
    .grid { grid-template-columns: 1fr; }
  }
  .row { display: flex; align-items: center; gap: 0.6rem; margin-top: 0.6rem; flex-wrap: wrap; }
  .ovr {
    font-size: 0.72em; color: var(--warn); background: var(--warn-bg);
    border-radius: 999px; padding: 0 0.4rem; margin-left: 0.3rem;
  }
  .ovr.env { color: var(--accent); background: var(--chip-bg); }
  .inline { display: inline-flex; align-items: center; gap: 0.3rem; }
  .actions { margin-left: 0.6rem; display: inline-flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
  .suggestions li { margin-bottom: 0.6rem; }
  .suggestions .actions { margin-left: 0; display: flex; }
  .mailacct strong { overflow-wrap: anywhere; }
  input:disabled { background: var(--disabled-bg); color: var(--disabled-text); cursor: not-allowed; }
  .ok { color: var(--ok); }
  .bad { color: var(--error); }
  .hint { color: var(--frozen-text); font-size: 0.9em; }
  .tablewrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .mailacct { border-top: 1px solid var(--table-border); padding-top: 0.5rem; margin-top: 0.5rem; }
  .mailacct .row { margin-top: 0; }
  .mailacct p { margin: 0.25rem 0 0; }
  table { border-collapse: collapse; margin-top: 0.5rem; }
  td, th { border: 1px solid var(--table-border); padding: 0.25rem 0.8rem; text-align: right; }
  tr.best td { background: var(--ok-bg); font-weight: 600; }
</style>
