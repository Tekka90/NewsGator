<script lang="ts">
  /** Readeck logo (official mark, simplified for small sizes).
   *  Uses currentColor for the book spine so it follows the button text color;
   *  the two accent panels keep Readeck's brand colors. */
  let { size = 16 }: { size?: number } = $props();
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 240 240"
  aria-hidden="true"
  focusable="false"
  style="display:inline-block;vertical-align:middle"
>
  <path
    fill="rgb(27.45%, 73.33%, 83.92%)"
    d="M 91.19 117.81 L 91.19 189.30 L 197.08 238.67 L 190.73 199.96 L 224.46 179.95 Z"
  />
  <path
    fill="currentColor"
    d="M 26.39 38.97 L 91.19 38.97 L 91.19 230.95 L 26.39 230.95 Z"
  />
  <path
    fill="rgb(27.45%, 73.33%, 83.92%)"
    d="M 91.19 3.22 L 26.39 38.97 L 218.39 128.49 L 218.39 57 Z"
  />
  <path
    fill="rgb(255, 255, 255)"
    fill-opacity="0.9"
    d="M 91.19 69.19 L 91.19 116.31 L 141.73 92.75 Z"
  />
</svg>
