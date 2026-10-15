<script setup lang="ts">
// local-docker only: "bundle the loop package into the Docker image" toggle,
// shared by the New channel form and the Configuration edit form on the
// channel detail page. Default off (volume mount) -- turning it on shows the
// trade-offs inline, since it changes what `start`/`refresh` build and run.
import Checkbox from 'primevue/checkbox'
import Message from 'primevue/message'
import FieldHelp from './FieldHelp.vue'

defineProps<{ inputId: string }>()
const model = defineModel<boolean>({ default: false })
</script>

<template>
  <div class="flex flex-column gap-2">
    <div class="flex align-items-center gap-2">
      <Checkbox v-model="model" binary :input-id="inputId" />
      <label :for="inputId" class="text-sm">Bundle content into the Docker image</label>
      <FieldHelp label="Bundle content into the Docker image">
        Off (default): the container bind-mounts the baked loop package from this machine, so
        updating content is just Spark + Refresh -- but the image on its own is useless to anyone
        else.<br /><br />
        On: the loop package is copied into a per-channel image
        (<code>its-a-live-&lt;name&gt;:baked</code>), which is then run with no mount. That image is
        fully self-contained -- <code>docker run -p 8080:8080 &lt;image&gt;</code> works anywhere,
        with no S3, AWS credentials or volume -- and can be exported from the channel page
        (&ldquo;Export shareable image&rdquo;).
      </FieldHelp>
    </div>
    <Message v-if="model" severity="warn" :closable="false" class="text-sm">
      <ul class="m-0 pl-3 line-height-3">
        <li>The image contains the <strong>entire loop</strong> (about the size of the source video); anyone with the image can extract it. Don't push it to a public registry unless you may publish the content.</li>
        <li>It is a <strong>snapshot</strong>: Spark alone changes nothing. Start / Refresh / Update content rebuild the image, which is slower than a volume mount and leaves old images behind until <code>docker image prune</code>.</li>
        <li>Applies to <strong>local-docker</strong> only; ecs-express keeps its content in S3.</li>
      </ul>
    </Message>
  </div>
</template>
