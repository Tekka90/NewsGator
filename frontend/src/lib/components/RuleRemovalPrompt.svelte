<script lang="ts">
  import { t } from '$lib/i18n';
  import { removalAsk } from '$lib/ruleRemoval';

  const choose = (keep: boolean | null) => $removalAsk?.resolve(keep);
</script>

<svelte:window onkeydown={(e) => e.key === 'Escape' && $removalAsk && choose(null)} />

{#if $removalAsk}
  <button class="bg" aria-label={$t('topics.close')} onclick={() => choose(null)} tabindex="-1"></button>
  <div class="dlg" role="alertdialog" aria-label={$t('topics.removeAskTitle', { count: $removalAsk.count })}>
    <p>{$t('topics.removeAskTitle', { count: $removalAsk.count })}</p>
    <div class="actions">
      <button class="primary" onclick={() => choose(false)}>{$t('topics.showAgain')}</button>
      <button onclick={() => choose(true)}>{$t('topics.keepHidden')}</button>
    </div>
  </div>
{/if}

<style>
  .bg {
    position: fixed;
    inset: 0;
    z-index: 300;
    border: 0;
    background: rgba(11, 61, 42, 0.45);
  }
  .dlg {
    position: fixed;
    z-index: 301;
    left: 50%;
    top: 50%;
    transform: translate(-50%, -50%);
    width: min(24rem, calc(100vw - 2rem));
    padding: 1.1rem;
    border-radius: var(--radius-card, 18px);
    background: var(--gator-paper, #f8f7f1);
    color: var(--gator-forest-900, #0b3d2a);
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.3);
  }
  p {
    margin: 0 0 0.9rem;
    font-weight: 600;
  }
  .actions {
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
  }
  button {
    border: 1px solid var(--gator-forest, #146b3a);
    border-radius: 999px;
    padding: 0.45rem 1rem;
    background: transparent;
    color: var(--gator-forest, #146b3a);
    font-weight: 600;
    cursor: pointer;
  }
  button.primary {
    background: var(--gator-forest, #146b3a);
    color: #fff;
  }
</style>
