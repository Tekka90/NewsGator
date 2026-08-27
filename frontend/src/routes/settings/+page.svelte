<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api';
  import { currentUser } from '$lib/stores';
  import type { Category, ManagedUser } from '$lib/types';

  let language = $state('');
  let saved = $state(false);
  let categories = $state<Category[]>([]);
  let newCategory = $state('');
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

  type Field = { key: string; label: string; secret?: boolean };
  // Grouped so each external service stays together with its test button.
  const sysGroups: { title: string; hint?: string; fields: Field[] }[] = [
    {
      title: 'LLM server',
      hint: 'External OpenAI-compatible server for summarization + embeddings.',
      fields: [
        { key: 'llm_base_url', label: 'LLM base URL' },
        { key: 'llm_model', label: 'LLM model' },
        { key: 'llm_api_key', label: 'LLM API key', secret: true },
        { key: 'embed_base_url', label: 'Embeddings base URL (empty = same as LLM)' },
        { key: 'embed_model', label: 'Embedding model' }
      ]
    },
    {
      title: 'Vector store',
      hint: 'Where article/story embeddings live. sqlite_vec needs nothing; qdrant is an external server.',
      fields: [
        { key: 'vector_backend', label: 'Vector backend (sqlite_vec | qdrant)' },
        { key: 'qdrant_url', label: 'Qdrant URL (external)' },
        { key: 'qdrant_api_key', label: 'Qdrant API key', secret: true }
      ]
    },
    {
      title: 'Readeck (optional)',
      hint: 'Self-hosted read-later archive. Set both to enable "Save to Readeck" on stories.',
      fields: [
        { key: 'readeck_base_url', label: 'Readeck base URL' },
        { key: 'readeck_token', label: 'Readeck API token', secret: true }
      ]
    },
    {
      title: 'Sharing',
      hint: 'Languages offered when sharing a story (comma-separated ISO codes, e.g. en,fr,de). Translation happens on demand via the LLM.',
      fields: [{ key: 'share_languages', label: 'Share languages' }]
    },
    {
      title: 'Clustering',
      hint: 'Tune via the feedback report below before changing thresholds.',
      fields: [
        { key: 'tau_attach', label: 'Clustering threshold τ_attach' },
        { key: 'tau_gray', label: 'Gray-zone threshold τ_gray' },
        { key: 'freeze_after_hours', label: 'Story freeze window (hours)' }
      ]
    },
    {
      title: 'Ingestion & retention',
      fields: [
        { key: 'retention_days', label: 'Retention (days)' },
        { key: 'feed_disable_after_days', label: 'Disable feed after N days of failures' },
        { key: 'feed_backfill_days', label: 'First-poll backfill window (days, 0 = all)' },
        { key: 'summary_language', label: 'Summary language (global default)' }
      ]
    }
  ];

  onMount(async () => {
    language = $currentUser?.summary_language ?? '';
    if ($currentUser?.is_admin) {
      categories = await api.categories.list();
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

  async function addCategory(e: SubmitEvent) {
    e.preventDefault();
    await api.categories.create(newCategory);
    newCategory = '';
    categories = await api.categories.list();
  }

  async function removeCategory(c: Category) {
    if (!confirm(`Delete category "${c.name}"?`)) return;
    await api.categories.remove(c.id);
    categories = await api.categories.list();
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
    const password = prompt(`New password for ${u.username} (min 8 chars):`);
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
    if (!confirm(`Delete user "${u.username}"? Their read state is lost.`)) return;
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

<h1>Settings</h1>

<div class="card">
  <h2>Your preferences</h2>
  <label>
    Summary language (ISO code, e.g. <code>en</code>, <code>fr</code>, <code>de</code>;
    empty = global default)
    <input bind:value={language} maxlength="8" placeholder="en" />
  </label>
  <button onclick={saveLanguage}>Save</button>
  {#if saved}<span class="ok">Saved ✓</span>{/if}
</div>

{#if $currentUser?.is_admin}
  <div class="card">
    <h2>Categories (admin)</h2>
    <form class="add" onsubmit={addCategory}>
      <input bind:value={newCategory} placeholder="New category" required />
      <button type="submit">Add</button>
    </form>
    <ul>
      {#each categories as c (c.id)}
        <li>
          {c.name}
          {#if c.name !== 'Uncategorized'}
            <button class="link" onclick={() => removeCategory(c)}>delete</button>
          {/if}
        </li>
      {/each}
    </ul>
  </div>

  <div class="card">
    <h2>Users (admin)</h2>
    <form class="add" onsubmit={addUser}>
      <input bind:value={newUsername} placeholder="Username" minlength="3" required />
      <input
        type="password"
        bind:value={newUserPassword}
        placeholder="Password"
        minlength="8"
        autocomplete="new-password"
        required
      />
      <label class="inline">
        <input type="checkbox" bind:checked={newUserAdmin} /> admin
      </label>
      <button type="submit">Add user</button>
    </form>
    {#if userError}<p class="bad">{userError}</p>{/if}
    <ul>
      {#each users as u (u.id)}
        <li>
          {u.username}
          {#if u.is_admin}<span class="ovr">admin</span>{/if}
          {#if u.id === $currentUser?.id}<span class="hint">(you)</span>{/if}
          <span class="actions">
            <button class="linkbtn" onclick={() => resetPassword(u)}>reset password</button>
            <button class="linkbtn" onclick={() => toggleAdmin(u)}>
              {u.is_admin ? 'revoke admin' : 'make admin'}
            </button>
            {#if u.id !== $currentUser?.id}
              <button class="link" onclick={() => removeUser(u)}>delete</button>
            {/if}
          </span>
        </li>
      {/each}
    </ul>
  </div>

  <div class="card">
    <h2>System (admin)</h2>
    {#each sysGroups as group (group.title)}
      <section class="group">
        <h3>{group.title}</h3>
        {#if group.hint}<p class="hint">{group.hint}</p>{/if}
        <div class="grid">
          {#each group.fields as f (f.key)}
            {@const locked = envLocked.includes(f.key)}
            <label title={locked ? `Set via environment variable ${f.key.toUpperCase()} — change it in your container/launch environment and restart` : undefined}>
              {f.label}
              {#if locked}
                <span class="ovr env">env</span>
              {:else if overridden.includes(f.key)}
                <span class="ovr">overridden</span>
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

        {#if group.title === 'LLM server'}
          <div class="row">
            <button class="linkbtn" onclick={testLlm}>Test connection</button>
            {#if llmTest}
              <span class:ok={llmTest.chat && llmTest.embeddings} class:bad={!llmTest.chat || !llmTest.embeddings}>
                chat: {llmTest.chat ? '✓' : '✗'} · embeddings: {llmTest.embeddings ? '✓' : '✗'}
                {#if llmTest.api_key_hint}· key in use: {llmTest.api_key_hint}{/if}
                {#each llmTest.errors as err}<br /><small>{err}</small>{/each}
              </span>
            {/if}
          </div>
        {:else if group.title === 'Vector store'}
          <div class="row">
            <button class="linkbtn" onclick={testQdrant} disabled={sys.vector_backend !== 'qdrant'}>
              Test Qdrant connection
            </button>
            {#if qdrantTest}
              <span class:ok={qdrantTest.ok} class:bad={!qdrantTest.ok}>
                {qdrantTest.ok ? `✓ reachable${qdrantTest.version?.version ? ' · v' + qdrantTest.version.version : ''}` : '✗ failed'}
                {#each qdrantTest.errors as err}<br /><small>{err}</small>{/each}
              </span>
            {/if}
          </div>
        {:else if group.title === 'Readeck (optional)'}
          <div class="row">
            <button class="linkbtn" onclick={testReadeck} disabled={!sys.readeck_base_url || !sys.readeck_token}>
              Test Readeck connection
            </button>
            {#if readeckTest}
              <span class:ok={readeckTest.ok} class:bad={!readeckTest.ok}>
                {readeckTest.ok ? `✓ connected as ${readeckTest.user}` : '✗ failed'}
                {#each readeckTest.errors as err}<br /><small>{err}</small>{/each}
              </span>
            {/if}
          </div>
        {/if}
      </section>
    {/each}

    <div class="row">
      <button onclick={saveSystem}>Save system settings</button>
      {#if sysSaved}<span class="ok">Saved ✓</span>{/if}
    </div>
  </div>

  <div class="card">
    <h2>Clustering feedback (admin)</h2>
    <p class="hint">
      Replays logged clustering decisions + your merge/split corrections against
      candidate thresholds. Suggestions are applied only if you confirm them above.
    </p>
    <button onclick={loadReport}>Generate report</button>
    {#if report}
      <p>
        {report.decisions_logged} decisions logged · {report.labeled_pairs} labeled corrections
        · current τ_attach = {report.current.tau_attach}
        {#if report.suggested_tau_attach}
          · <strong>suggested τ_attach = {report.suggested_tau_attach}</strong>
        {:else}
          · not enough labeled data for a suggestion
        {/if}
      </p>
      {#if report.candidates.length}
        <div class="tablewrap">
          <table>
            <thead><tr><th>τ</th><th>precision</th><th>recall</th><th>F1</th></tr></thead>
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
  .link {
    background: none;
    border: none;
    color: var(--error);
    text-decoration: underline;
    padding: 0;
  }
  code { background: var(--code-bg); padding: 0 0.25rem; border-radius: 4px; }
  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0 1.5rem;
  }
  .grid input { width: 100%; }
  @media (max-width: 700px) {
    .grid { grid-template-columns: 1fr; }
  }
  .row { display: flex; align-items: center; gap: 0.6rem; margin-top: 0.6rem; }
  .ovr {
    font-size: 0.72em; color: var(--warn); background: var(--warn-bg);
    border-radius: 999px; padding: 0 0.4rem; margin-left: 0.3rem;
  }
  .ovr.env { color: var(--accent); background: var(--chip-bg); }
  .inline { display: inline-flex; align-items: center; gap: 0.3rem; }
  .actions { margin-left: 0.6rem; display: inline-flex; gap: 0.5rem; align-items: center; }
  input:disabled { background: var(--disabled-bg); color: var(--disabled-text); cursor: not-allowed; }
  .ok { color: var(--ok); }
  .bad { color: var(--error); }
  .hint { color: var(--frozen-text); font-size: 0.9em; }
  .tablewrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { border-collapse: collapse; margin-top: 0.5rem; }
  td, th { border: 1px solid var(--table-border); padding: 0.25rem 0.8rem; text-align: right; }
  tr.best td { background: var(--ok-bg); font-weight: 600; }
</style>
