<script lang="ts">
  import { currentUser } from '$lib/stores';
</script>

<h1>Stories</h1>
<div class="card">
  <p>
    Welcome, {$currentUser?.username}. Stories will appear here once ingestion and
    clustering are implemented (Milestones 2–4). For now, add your first feeds on the
    <a href="/feeds">Feeds page</a>.
  </p>
</div>
