// SPDX-License-Identifier: GPL-2.0
/*
 * Test-only fault injection for multikernel boot-contract rejection paths.
 *
 * Each module load arms exactly one mutation.  The harness must run every
 * mode in a fresh, isolated QEMU instance.  In particular, bad_magic makes
 * the selected CPU enter mk_reject_spawn_context().  That CPU stays local
 * and inert because it cannot trust the host park context.
 *
 * Expected external assertions:
 *
 * bad_magic:
 *   The child rejects the spawn context before normal kernel startup.  Its
 *   CPU is deliberately unreclaimable, so discard QEMU after observing the
 *   injected status and absence of child startup.
 * parent_mismatch:
 *   mk_restore_host_instance() reports an invalid parent/child IPI identity.
 * parent_missing:
 *   mk_restore_host_instance() reports no parent IPI CPU in the boot tree.
 *
 * For both parent metadata modes, the harness must halt the child, confirm
 * its assigned CPU parked, then prove the CPU can be reloaded or reassigned.
 */
#include <linux/atomic.h>
#include <linux/kprobes.h>
#include <linux/module.h>
#include <linux/string.h>

#include <linux/multikernel.h>

enum mk_reject_status {
	MK_REJECT_STATUS_IDLE,
	MK_REJECT_STATUS_ARMED,
	MK_REJECT_STATUS_INJECTED,
	MK_REJECT_STATUS_ERROR,
};

static char *mode;
module_param(mode, charp, 0444);
MODULE_PARM_DESC(mode,
		 "Fault mode: bad_magic, parent_mismatch, or parent_missing");

static int fired;
module_param(fired, int, 0444);
static int status;
module_param(status, int, 0444);
static unsigned long long original_value;
module_param(original_value, ullong, 0444);
static unsigned long long injected_value;
module_param(injected_value, ullong, 0444);

static atomic_t injection_claimed = ATOMIC_INIT(0);

/* Same length is unnecessary, but makes a manifest dump easy to compare. */
static const char missing_parent_property[] =
	"multikernel,test-no-parent";

static void mk_reject_record(u64 original, u64 injected)
{
	WRITE_ONCE(original_value, original);
	WRITE_ONCE(injected_value, injected);
	WRITE_ONCE(fired, 1);
	WRITE_ONCE(status, MK_REJECT_STATUS_INJECTED);
	pr_info("MK_REJECT_CONTRACT_INJECT mode=%s original=%#llx injected=%#llx\n",
		mode, original, injected);
}

static int mk_bad_magic_pre(struct kprobe *probe, struct pt_regs *regs)
{
	struct mk_spawn_context *ctx = (void *)regs->dx;
	u32 original;
	u32 injected;

	(void)probe;
	if (atomic_cmpxchg(&injection_claimed, 0, 1))
		return 0;
	if (!ctx) {
		WRITE_ONCE(status, MK_REJECT_STATUS_ERROR);
		return 0;
	}

	original = READ_ONCE(ctx->abi_magic);
	injected = original ^ 1;
	if (injected == MK_BOOT_CONTEXT_MAGIC)
		injected ^= 2;
	WRITE_ONCE(ctx->abi_magic, injected);
	mk_reject_record(original, injected);
	return 0;
}

static int mk_parent_mismatch_pre(struct kprobe *probe, struct pt_regs *regs)
{
	mk_phys_cpu_t original = regs->cx;
	mk_phys_cpu_t injected;

	(void)probe;
	if (atomic_cmpxchg(&injection_claimed, 0, 1))
		return 0;

	injected = original ^ 1;
	if (injected == MK_PHYS_CPU_INVALID)
		injected ^= 2;
	regs->cx = injected;
	mk_reject_record(original, injected);
	return 0;
}

static int mk_parent_missing_pre(struct kprobe *probe, struct pt_regs *regs)
{
	const char *name = (const char *)regs->si;

	(void)probe;
	if (!name || strcmp(name, "multikernel,host-ipi-cpu"))
		return 0;
	if (atomic_cmpxchg(&injection_claimed, 0, 1))
		return 0;

	regs->si = (unsigned long)missing_parent_property;
	mk_reject_record(1, 0);
	return 0;
}

static struct kprobe reject_probe;

static int __init mk_reject_contract_init(void)
{
	int ret;

	if (!mode)
		return -EINVAL;

	if (!strcmp(mode, "bad_magic")) {
		reject_probe.symbol_name = "mk_spawn_cpu";
		reject_probe.pre_handler = mk_bad_magic_pre;
	} else if (!strcmp(mode, "parent_mismatch")) {
		reject_probe.symbol_name = "mk_ipi_link_reset";
		reject_probe.pre_handler = mk_parent_mismatch_pre;
	} else if (!strcmp(mode, "parent_missing")) {
		reject_probe.symbol_name = "fdt_property";
		reject_probe.pre_handler = mk_parent_missing_pre;
	} else {
		pr_err("MK_REJECT_CONTRACT_ERROR unknown mode=%s\n", mode);
		return -EINVAL;
	}

	WRITE_ONCE(status, MK_REJECT_STATUS_ARMED);
	ret = register_kprobe(&reject_probe);
	if (ret) {
		WRITE_ONCE(status, MK_REJECT_STATUS_ERROR);
		return ret;
	}

	pr_info("MK_REJECT_CONTRACT_READY mode=%s symbol=%s\n",
		mode, reject_probe.symbol_name);
	return 0;
}

static void __exit mk_reject_contract_exit(void)
{
	unregister_kprobe(&reject_probe);
	pr_info("MK_REJECT_CONTRACT_RESULT mode=%s fired=%d status=%d original=%#llx injected=%#llx\n",
		mode, fired, status, original_value, injected_value);
}

module_init(mk_reject_contract_init);
module_exit(mk_reject_contract_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Multikernel boot-contract rejection fault fixture");
