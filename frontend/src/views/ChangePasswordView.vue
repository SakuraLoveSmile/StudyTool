<template>
  <div class="mx-auto flex min-h-[72vh] max-w-md flex-col items-center justify-center px-2">
    <ThemeToggle class="fixed right-4 top-4 z-40" />
    <div class="mb-8 flex flex-col items-center text-center">
      <div class="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-brand-700 text-white shadow-lift shadow-brand-600/30">
        <KeyRound class="h-7 w-7" />
      </div>
      <h1 class="font-display text-2xl font-bold tracking-tight text-ink sm:text-3xl">设置新密码</h1>
      <p class="mt-2 text-sm text-sub">首次登录需要设置新密码</p>
    </div>

    <div class="surface w-full p-8">
      <form class="space-y-4" @submit.prevent="submit">
        <div>
          <label class="field-label" for="cp-current">当前密码</label>
          <input id="cp-current" v-model="currentPassword" required type="password" autocomplete="current-password" class="field" />
        </div>
        <div>
          <label class="field-label" for="cp-new">新密码</label>
          <input id="cp-new" v-model="newPassword" required minlength="8" type="password" autocomplete="new-password" class="field" />
          <p class="mt-1 text-[11px] text-faint">至少 8 个字符</p>
        </div>
        <p v-if="error" role="alert" class="rounded-xl border border-err/30 bg-err/10 px-3 py-2 text-sm text-err">{{ error }}</p>
        <button type="submit" :disabled="loading" class="btn btn-primary w-full">{{ loading ? '保存中...' : '保存新密码' }}</button>
      </form>
    </div>
  </div>
</template>
<script setup lang="ts">
import { ref } from 'vue'; import { useRoute, useRouter } from 'vue-router'; import { KeyRound } from 'lucide-vue-next'; import { useAuth } from '../composables/useAuth'; import ThemeToggle from '../components/ThemeToggle.vue';
const currentPassword = ref(''); const newPassword = ref(''); const error = ref(''); const router = useRouter(); const route = useRoute(); const auth = useAuth(); const loading = auth.loading;
const submit = async () => { error.value = ''; try { await auth.changePassword(currentPassword.value, newPassword.value); await router.replace(String(route.query.redirect || '/today')); } catch (e) { error.value = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '修改密码失败'; } };
</script>
