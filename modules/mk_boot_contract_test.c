// SPDX-License-Identifier: GPL-2.0
/*
 * Fixture for the boot-window transport contract.
 *
 * mk_arch_spawn_instance() is entered by the caller after mk_ipi_link_reset()
 * has initialized the endpoint and before mk_spawn_cpu() releases the child.
 * The one-shot probe opens that window, lets a helper on another CPU send a
 * real SYSTEM/SHUTDOWN message, and holds the probed instruction until the
 * helper has completed.
 */
#include <linux/atomic.h>
#include <linux/completion.h>
#include <linux/ktime.h>
#include <linux/kprobes.h>
#include <linux/kthread.h>
#include <linux/module.h>
#include <linux/multikernel.h>
#include <linux/smp.h>

#define MK_BOOT_CONTRACT_TIMEOUT_MS 5000

static int target_id = 1;
module_param(target_id, int, 0444);
static int helper_cpu = 1;
module_param(helper_cpu, int, 0444);

static int result;
module_param(result, int, 0444);
static int send_ret;
module_param(send_ret, int, 0444);
static int fired;
module_param(fired, int, 0444);
static int acked;
module_param(acked, int, 0444);
static int status;
module_param(status, int, 0444);

static struct task_struct *sender_task;
static DECLARE_COMPLETION(send_gate);
static DECLARE_COMPLETION(send_done);
static DECLARE_COMPLETION(stop_gate);
static atomic_t probe_claimed = ATOMIC_INIT(0);

enum mk_boot_contract_status {
	MK_BOOT_CONTRACT_IDLE = 0,
	MK_BOOT_CONTRACT_WAITING = 1,
	MK_BOOT_CONTRACT_SENT = 2,
	MK_BOOT_CONTRACT_TIMEOUT = 3,
	MK_BOOT_CONTRACT_ERROR = 4,
};

static atomic_t outcome = ATOMIC_INIT(MK_BOOT_CONTRACT_IDLE);

static int mk_boot_contract_sender(void *unused)
{
	struct mk_shutdown_payload payload = {
		.flags = MK_SHUTDOWN_GRACEFUL,
		.sender_instance_id = 0,
	};
	int ret;

	wait_for_completion(&send_gate);
	if (kthread_should_stop())
		return 0;

	ret = mk_send_message(target_id, MK_MSG_SYSTEM, MK_SYS_SHUTDOWN,
			      &payload, sizeof(payload));
	WRITE_ONCE(send_ret, ret);
	if (ret) {
		if (atomic_cmpxchg(&outcome, MK_BOOT_CONTRACT_WAITING,
				   MK_BOOT_CONTRACT_ERROR) ==
		    MK_BOOT_CONTRACT_WAITING) {
			WRITE_ONCE(status, MK_BOOT_CONTRACT_ERROR);
			WRITE_ONCE(result, ret);
		}
	} else if (atomic_cmpxchg(&outcome, MK_BOOT_CONTRACT_WAITING,
				  MK_BOOT_CONTRACT_SENT) ==
		   MK_BOOT_CONTRACT_WAITING) {
		WRITE_ONCE(status, MK_BOOT_CONTRACT_SENT);
		WRITE_ONCE(result, 1);
	}
	complete(&send_done);
	/* Keep sender_task valid until module exit calls kthread_stop(). */
	wait_for_completion(&stop_gate);
	return 0;
}

static int mk_boot_contract_pre(struct kprobe *probe, struct pt_regs *regs)
{
	u64 deadline;

	(void)probe;
	(void)regs;
	if (atomic_cmpxchg(&probe_claimed, 0, 1))
		return 0;

	WRITE_ONCE(fired, 1);
	if (raw_smp_processor_id() == helper_cpu) {
		atomic_set(&outcome, MK_BOOT_CONTRACT_ERROR);
		WRITE_ONCE(status, MK_BOOT_CONTRACT_ERROR);
		WRITE_ONCE(result, -EINVAL);
		complete(&send_done);
		return 0;
	}
	atomic_set(&outcome, MK_BOOT_CONTRACT_WAITING);
	WRITE_ONCE(status, MK_BOOT_CONTRACT_WAITING);
	complete(&send_gate);

	/*
	 * Kprobe handlers cannot sleep. The helper is bound to another CPU, so
	 * a bounded cpu_relax loop keeps this fixture safe in the handler while
	 * making the send ordering explicit before the probed instruction runs.
	 */
	deadline = ktime_get_ns() +
		(u64)MK_BOOT_CONTRACT_TIMEOUT_MS * NSEC_PER_MSEC;
	while (!completion_done(&send_done) && ktime_get_ns() < deadline)
		cpu_relax();

	if (!completion_done(&send_done) &&
	    atomic_cmpxchg(&outcome, MK_BOOT_CONTRACT_WAITING,
			   MK_BOOT_CONTRACT_TIMEOUT) ==
	    MK_BOOT_CONTRACT_WAITING) {
		WRITE_ONCE(status, MK_BOOT_CONTRACT_TIMEOUT);
		WRITE_ONCE(result, -ETIMEDOUT);
		complete(&send_done);
	}
	return 0;
}

static struct kprobe mk_boot_contract_probe = {
	.symbol_name = "mk_arch_spawn_instance",
	.pre_handler = mk_boot_contract_pre,
};

static int mk_boot_contract_ack_pre(struct kprobe *probe, struct pt_regs *regs)
{
	(void)probe;
	if ((u32)regs->di != MK_MSG_SYSTEM ||
	    (u32)regs->si != MK_SYS_SHUTDOWN ||
	    (u64)regs->dx != target_id || (int)regs->cx)
		return 0;

	WRITE_ONCE(acked, 1);
	pr_info("MK_BOOT_CONTRACT_ACK target=%d result=0\n", target_id);
	return 0;
}

static struct kprobe mk_boot_contract_ack_probe = {
	.symbol_name = "mk_msg_pending_complete",
	.pre_handler = mk_boot_contract_ack_pre,
};

static int __init mk_boot_contract_init(void)
{
	int ret;

	if (helper_cpu < 0 || helper_cpu >= nr_cpu_ids ||
	    !cpu_online(helper_cpu))
		return -EINVAL;

	sender_task = kthread_create(mk_boot_contract_sender, NULL, "mkboot");
	if (IS_ERR(sender_task)) {
		ret = PTR_ERR(sender_task);
		sender_task = NULL;
		return ret;
	}
	kthread_bind(sender_task, helper_cpu);
	wake_up_process(sender_task);

	ret = register_kprobe(&mk_boot_contract_ack_probe);
	if (ret)
		goto stop_sender;

	ret = register_kprobe(&mk_boot_contract_probe);
	if (ret) {
		unregister_kprobe(&mk_boot_contract_ack_probe);
		goto stop_sender;
	}
	pr_info("MK_BOOT_CONTRACT_READY target=%d helper_cpu=%d\n",
		target_id, helper_cpu);
	return 0;

stop_sender:
	complete(&send_gate);
	complete(&stop_gate);
	kthread_stop(sender_task);
	sender_task = NULL;
	return ret;
}

static void __exit mk_boot_contract_exit(void)
{
	unregister_kprobe(&mk_boot_contract_probe);
	unregister_kprobe(&mk_boot_contract_ack_probe);
	complete(&send_gate);
	complete(&stop_gate);
	if (sender_task)
		kthread_stop(sender_task);
	pr_info("MK_BOOT_CONTRACT_RESULT fired=%d acked=%d status=%d send_ret=%d result=%d\n",
		fired, acked, status, send_ret, result);
}

module_init(mk_boot_contract_init);
module_exit(mk_boot_contract_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Multikernel boot-window transport contract fixture");
