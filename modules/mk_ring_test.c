// SPDX-License-Identifier: GPL-2.0
#include <linux/completion.h>
#include <linux/delay.h>
#include <linux/errno.h>
#include <linux/kthread.h>
#include <linux/module.h>
#include <linux/multikernel.h>
#include <linux/slab.h>
#include <linux/string.h>

#define MK_RING_TEST_MAGIC 0x4d4b5254U
#define MK_RING_TEST_TYPE_ZERO (MK_MSG_USER + 0x100)
#define MK_RING_TEST_TYPE_DATA (MK_MSG_USER + 0x101)
#define MK_RING_TEST_TYPE_ACK (MK_MSG_USER + 0x102)
#define MK_RING_TEST_TYPE_FAIR (MK_MSG_USER + 0x103)
#define MK_RING_TEST_TYPE_DUMMY (MK_MSG_USER + 0x104)
#define MK_RING_FIFO_COUNT 130
#define MK_RING_CONCURRENT_COUNT 96
#define MK_RING_FULL_LIMIT 128
#define MK_RING_BLOCK_MS 5000
#define MK_RING_FAIR_MIN_COUNT 128
#define MK_RING_FAIR_TIMEOUT_MS 120000
#define MK_RING_BOOT_TIMEOUT_MS 180000

enum mk_ring_phase {
	MK_RING_PHASE_MAX = 1,
	MK_RING_PHASE_FIFO,
	MK_RING_PHASE_CONCURRENT,
	MK_RING_PHASE_FULL_BEGIN,
	MK_RING_PHASE_FULL_DATA,
	MK_RING_PHASE_FULL_SUMMARY,
	MK_RING_PHASE_FAIR_BEGIN,
	MK_RING_PHASE_FAIR_DATA,
	MK_RING_PHASE_FAIR_PEER_READY,
	MK_RING_PHASE_FAIR_PEER,
	MK_RING_PHASE_OVERSIZE_FOLLOWUP,
	MK_RING_PHASE_FAIR_PEER_START,
	MK_RING_PHASE_FAIR_STOP,
};

struct mk_ring_packet {
	u32 magic;
	u32 phase;
	u32 producer;
	u32 sequence;
	u32 value0;
	u32 value1;
};

struct mk_ring_sender {
	struct task_struct *task;
	struct completion done;
	u32 phase;
	u32 producer;
	u32 count;
	u32 successes;
	u32 enospc;
	int error;
};

static char *role = "host";
module_param(role, charp, 0444);
static int target_id = 1;
module_param(target_id, int, 0444);
static int peer_id = 2;
module_param(peer_id, int, 0444);
static int result;
module_param(result, int, 0444);

static struct mk_ipi_handler *zero_handler;
static struct mk_ipi_handler *data_handler;
static struct mk_ipi_handler *fair_handler;
static struct mk_ipi_handler *dummy_handler;
static bool ack_registered;
static struct task_struct *child_task;
static struct task_struct *host_task;
static DECLARE_COMPLETION(full_ack);
static DECLARE_COMPLETION(fair_stop);
static DECLARE_COMPLETION(fair_peer_start);
static DECLARE_COMPLETION(base_done);
static DECLARE_COMPLETION(fair_begin);
static DECLARE_COMPLETION(fair_peer_ready);
static DECLARE_COMPLETION(fair_peer);
static DECLARE_COMPLETION(oversize_followup);
static atomic_t fair_count = ATOMIC_INIT(0);

struct mk_ring_host_state {
	u32 fifo_next;
	u32 concurrent_next[2];
	u32 full_next[2];
	u32 full_sent;
	u32 full_enospc;
	bool saw_zero;
	bool saw_max;
	bool saw_full_begin;
	bool base_passed;
};

static struct mk_ring_host_state host_state;

static void mk_ring_fail(int error, const char *reason)
{
	if (!error)
		error = -EINVAL;
	if (!READ_ONCE(result)) {
		WRITE_ONCE(result, error);
		pr_err("MK_RING_TEST_FAIL role=%s reason=%s error=%d\n",
		       role, reason, error);
	}
}

static u8 mk_ring_pattern(size_t offset)
{
	return (u8)((offset * 33U + 17U) & 0xff);
}

static int mk_ring_send(void *data, size_t size, unsigned long type, bool retry)
{
	unsigned int attempts = 0;
	int ret;

	do {
		ret = multikernel_send_ipi_data_to_host(data, size, type);
		if (ret != -ENOSPC || !retry)
			return ret;
		usleep_range(1000, 2000);
	} while (++attempts < 10000 && !kthread_should_stop());
	return ret ?: -ETIMEDOUT;
}

static void mk_ring_zero_callback(struct mk_ipi_data *slot, void *context)
{
	if (READ_ONCE(slot->data_size) != 0)
		mk_ring_fail(-EMSGSIZE, "zero-size");
	else if (host_state.saw_zero)
		mk_ring_fail(-EEXIST, "zero-duplicate");
	else
		host_state.saw_zero = true;
}

static bool mk_ring_packet_valid(struct mk_ipi_data *slot,
				 struct mk_ring_packet **packet)
{
	size_t size = READ_ONCE(slot->data_size);

	if (size < sizeof(**packet) || size > MK_MAX_DATA_SIZE) {
		mk_ring_fail(-EMSGSIZE, "packet-size");
		return false;
	}
	*packet = (struct mk_ring_packet *)slot->buffer;
	if ((*packet)->magic != MK_RING_TEST_MAGIC) {
		mk_ring_fail(-EBADMSG, "packet-magic");
		return false;
	}
	return true;
}

static void mk_ring_validate_max(struct mk_ipi_data *slot)
{
	struct mk_ring_packet *packet;
	size_t i;

	if (READ_ONCE(slot->data_size) != MK_MAX_DATA_SIZE ||
	    !mk_ring_packet_valid(slot, &packet))
		return;
	for (i = sizeof(*packet); i < MK_MAX_DATA_SIZE; i++) {
		if (slot->buffer[i] != mk_ring_pattern(i)) {
			mk_ring_fail(-EBADMSG, "max-pattern");
			return;
		}
	}
	if (host_state.saw_max)
		mk_ring_fail(-EEXIST, "max-duplicate");
	else
		host_state.saw_max = true;
}

static void mk_ring_finish(struct mk_ring_packet *packet)
{
	u32 sent = packet->value0;
	u32 enospc = packet->value1;

	if (!host_state.saw_zero || !host_state.saw_max ||
	    host_state.fifo_next != MK_RING_FIFO_COUNT ||
	    host_state.concurrent_next[0] != MK_RING_CONCURRENT_COUNT ||
	    host_state.concurrent_next[1] != MK_RING_CONCURRENT_COUNT ||
	    !host_state.saw_full_begin ||
	    host_state.full_next[0] + host_state.full_next[1] != sent ||
	    sent != MK_IPI_RING_SIZE - 1 || !enospc) {
		mk_ring_fail(-EINVAL, "summary");
		return;
	}
	host_state.full_sent = sent;
	host_state.full_enospc = enospc;
	host_state.base_passed = true;
	complete(&base_done);
}

static void mk_ring_data_callback(struct mk_ipi_data *slot, void *context)
{
	struct mk_ring_packet *packet;
	struct mk_resource_ack ack = {
		.operation = MK_RING_TEST_TYPE_ACK,
	};
	u32 producer;

	if (READ_ONCE(result) < 0)
		return;
	if (READ_ONCE(slot->data_size) == MK_MAX_DATA_SIZE) {
		mk_ring_validate_max(slot);
		return;
	}
	if (!mk_ring_packet_valid(slot, &packet))
		return;
	producer = packet->producer;
	switch (packet->phase) {
	case MK_RING_PHASE_FIFO:
		if (producer || packet->sequence != host_state.fifo_next)
			mk_ring_fail(-ERANGE, "fifo-order");
		else
			host_state.fifo_next++;
		break;
	case MK_RING_PHASE_CONCURRENT:
		if (producer > 1 ||
		    packet->sequence != host_state.concurrent_next[producer])
			mk_ring_fail(-ERANGE, "concurrent-order");
		else
			host_state.concurrent_next[producer]++;
		break;
	case MK_RING_PHASE_FULL_BEGIN:
		if (host_state.saw_full_begin) {
			mk_ring_fail(-EEXIST, "full-begin-duplicate");
			return;
		}
		host_state.saw_full_begin = true;
		if (mk_send_message(target_id, MK_RING_TEST_TYPE_ACK,
				    MK_RING_PHASE_FULL_BEGIN,
				    &ack, sizeof(ack))) {
			mk_ring_fail(-EIO, "full-ack");
			return;
		}
		msleep(MK_RING_BLOCK_MS);
		break;
	case MK_RING_PHASE_FULL_DATA:
		if (producer > 1 ||
		    packet->sequence != host_state.full_next[producer])
			mk_ring_fail(-ERANGE, "full-order");
		else
			host_state.full_next[producer]++;
		break;
	case MK_RING_PHASE_FULL_SUMMARY:
		mk_ring_finish(packet);
		break;
	case MK_RING_PHASE_FAIR_BEGIN:
		complete(&fair_begin);
		break;
	case MK_RING_PHASE_FAIR_PEER_READY:
		complete(&fair_peer_ready);
		break;
	case MK_RING_PHASE_FAIR_PEER:
		complete(&fair_peer);
		break;
	case MK_RING_PHASE_OVERSIZE_FOLLOWUP:
		complete(&oversize_followup);
		break;
	default:
		mk_ring_fail(-EINVAL, "phase");
	}
}

static void mk_ring_ack_callback(u32 msg_type, u32 subtype,
				 void *payload, u32 payload_len,
				 s32 sender_instance_id, void *context)
{
	(void)sender_instance_id;
	if (msg_type != MK_RING_TEST_TYPE_ACK)
		return;
	if (subtype == MK_RING_PHASE_FULL_BEGIN)
		complete(&full_ack);
	else if (subtype == MK_RING_PHASE_FAIR_PEER_START)
		complete(&fair_peer_start);
	else if (subtype == MK_RING_PHASE_FAIR_STOP)
		complete(&fair_stop);
}

static void mk_ring_fair_callback(struct mk_ipi_data *slot, void *context)
{
	struct mk_ring_packet *packet;

	if (!mk_ring_packet_valid(slot, &packet))
		return;
	if (packet->phase != MK_RING_PHASE_FAIR_DATA) {
		mk_ring_fail(-EINVAL, "fair-phase");
		return;
	}
	/* Keep the ring hot long enough to expose an unbounded drain pass. */
	udelay(200);
	atomic_inc(&fair_count);
}

static void mk_ring_dummy_callback(struct mk_ipi_data *slot, void *context)
{
}

static int mk_ring_send_packet(u32 phase, u32 producer, u32 sequence,
			       u32 value0, u32 value1, bool retry)
{
	struct mk_ring_packet packet = {
		.magic = MK_RING_TEST_MAGIC,
		.phase = phase,
		.producer = producer,
		.sequence = sequence,
		.value0 = value0,
		.value1 = value1,
	};
	return mk_ring_send(&packet, sizeof(packet), MK_RING_TEST_TYPE_DATA, retry);
}

static int mk_ring_send_fair(u32 sequence)
{
	struct mk_ring_packet packet = {
		.magic = MK_RING_TEST_MAGIC,
		.phase = MK_RING_PHASE_FAIR_DATA,
		.sequence = sequence,
	};

	return mk_ring_send(&packet, sizeof(packet), MK_RING_TEST_TYPE_FAIR,
			    false);
}

static int mk_ring_sender_thread(void *argument)
{
	struct mk_ring_sender *sender = argument;
	u32 sequence;
	int ret;

	for (sequence = 0; sequence < sender->count; sequence++) {
		ret = mk_ring_send_packet(sender->phase, sender->producer,
					  sequence, 0, 0,
					  sender->phase != MK_RING_PHASE_FULL_DATA);
		if (!ret) {
			sender->successes++;
			continue;
		}
		if (ret == -ENOSPC &&
		    sender->phase == MK_RING_PHASE_FULL_DATA) {
			sender->enospc++;
			break;
		}
		sender->error = ret;
		break;
	}
	complete(&sender->done);
	return 0;
}

static int mk_ring_run_pair(u32 phase, u32 count,
			    struct mk_ring_sender senders[2])
{
	unsigned int i;
	int ret = 0;

	for (i = 0; i < 2; i++) {
		senders[i].phase = phase;
		senders[i].producer = i;
		senders[i].count = count;
		init_completion(&senders[i].done);
		senders[i].task = kthread_run(mk_ring_sender_thread, &senders[i],
					      "mk_ring_%u_%u", phase, i);
		if (IS_ERR(senders[i].task)) {
			ret = PTR_ERR(senders[i].task);
			senders[i].task = NULL;
			goto stop;
		}
	}
	for (i = 0; i < 2; i++) {
		if (!wait_for_completion_timeout(&senders[i].done,
						 msecs_to_jiffies(30000))) {
			ret = -ETIMEDOUT;
			goto stop;
		}
	}
	for (i = 0; i < 2; i++) {
		if (senders[i].error)
			return senders[i].error;
	}
	return 0;

stop:
	for (i = 0; i < 2; i++) {
		if (senders[i].task)
			kthread_stop(senders[i].task);
	}
	return ret;
}

static int mk_ring_host_thread(void *unused)
{
	struct mk_resource_ack ack = {
		.operation = MK_RING_TEST_TYPE_ACK,
	};
	unsigned long deadline;
	int count;
	int ret;

	if (!wait_for_completion_timeout(&base_done,
			msecs_to_jiffies(MK_RING_BOOT_TIMEOUT_MS))) {
		mk_ring_fail(-ETIMEDOUT, "base-ring-timeout");
		return 0;
	}
	if (!wait_for_completion_timeout(&fair_begin,
			msecs_to_jiffies(MK_RING_FAIR_TIMEOUT_MS))) {
		mk_ring_fail(-ETIMEDOUT, "fair-begin-timeout");
		return 0;
	}
	deadline = jiffies + msecs_to_jiffies(MK_RING_FAIR_TIMEOUT_MS);
	while (atomic_read(&fair_count) < MK_RING_FAIR_MIN_COUNT) {
		if (time_after(jiffies, deadline)) {
			mk_ring_fail(-ETIMEDOUT, "fair-refill-timeout");
			return 0;
		}
		usleep_range(1000, 2000);
	}
	/* The peer registers its handler before publishing READY. */
	if (!wait_for_completion_timeout(&fair_peer_ready,
			msecs_to_jiffies(MK_RING_FAIR_TIMEOUT_MS))) {
		mk_ring_fail(-ETIMEDOUT, "fair-peer-ready-timeout");
		return 0;
	}
	pr_info("MK_RING_TEST_FAIR_PEER_READY count=%d\n",
		atomic_read(&fair_count));
	deadline = jiffies + msecs_to_jiffies(MK_RING_FAIR_TIMEOUT_MS);
	do {
		ret = mk_send_message(peer_id, MK_RING_TEST_TYPE_ACK,
				      MK_RING_PHASE_FAIR_PEER_START,
				      &ack, sizeof(ack));
		if (!ret)
			break;
		usleep_range(1000, 2000);
	} while (time_before(jiffies, deadline));
	if (ret) {
		mk_ring_fail(ret, "fair-peer-start");
		return 0;
	}
	pr_info("MK_RING_TEST_FAIR_PEER_REQUEST count=%d\n",
		atomic_read(&fair_count));
	if (!wait_for_completion_timeout(&fair_peer,
			msecs_to_jiffies(MK_RING_FAIR_TIMEOUT_MS))) {
		mk_ring_fail(-ETIMEDOUT, "fair-peer-timeout");
		return 0;
	}
	pr_info("MK_RING_TEST_FAIR_PEER_PASS count=%d\n",
		atomic_read(&fair_count));
	multikernel_unregister_handler(dummy_handler);
	dummy_handler = NULL;
	pr_info("MK_RING_TEST_FAIR_UNREGISTER_PASS count=%d\n",
		atomic_read(&fair_count));
	ret = mk_send_message(target_id, MK_RING_TEST_TYPE_ACK,
			      MK_RING_PHASE_FAIR_STOP, &ack, sizeof(ack));
	if (ret) {
		mk_ring_fail(ret, "fair-stop-ack");
		return 0;
	}
	if (!wait_for_completion_timeout(&oversize_followup,
			msecs_to_jiffies(MK_RING_FAIR_TIMEOUT_MS))) {
		mk_ring_fail(-ETIMEDOUT, "oversize-followup-timeout");
		return 0;
	}
	count = atomic_read(&fair_count);
	if (!host_state.base_passed || count < MK_RING_FAIR_MIN_COUNT) {
		mk_ring_fail(-EINVAL, "fair-summary");
		return 0;
	}
	WRITE_ONCE(result, 1);
	pr_info("MK_RING_TEST_PASS zero=1 max=%u fifo=%u concurrent=%u full_sent=%u enospc=%u fairness=%d unregister=1 peer=1 oversized=1\n",
		MK_MAX_DATA_SIZE, MK_RING_FIFO_COUNT,
		2 * MK_RING_CONCURRENT_COUNT, host_state.full_sent,
		host_state.full_enospc, count);
	return 0;
}

static int mk_ring_inject_oversize(void)
{
	struct mk_instance *instance = READ_ONCE(host_instance);
	struct mk_ipi_endpoint *endpoint;
	struct mk_ipi_data *slot;
	unsigned long flags;
	u32 idx;

	if (!instance)
		return -ENODEV;
	endpoint = &instance->ipi_endpoint;
	raw_spin_lock_irqsave(&endpoint->tx_lock, flags);
	if (!READ_ONCE(endpoint->registered) || !endpoint->tx_enabled) {
		raw_spin_unlock_irqrestore(&endpoint->tx_lock, flags);
		return -ESHUTDOWN;
	}
	idx = endpoint->tx_head & (MK_IPI_RING_SIZE - 1);
	slot = &endpoint->tx->entries[idx];
	/* Pair with the receiver when it makes this raw test slot reusable. */
	if (smp_load_acquire(&slot->ready)) {
		raw_spin_unlock_irqrestore(&endpoint->tx_lock, flags);
		return -ENOSPC;
	}
	WRITE_ONCE(slot->sender_cpu, 0);
	WRITE_ONCE(slot->type, MK_RING_TEST_TYPE_FAIR);
	WRITE_ONCE(slot->data_size, MK_MAX_DATA_SIZE + 1);
	/* Publish the deliberately invalid size with the rest of the slot. */
	smp_store_release(&slot->ready, 1);
	endpoint->tx_head++;
	raw_spin_unlock_irqrestore(&endpoint->tx_lock, flags);
	return 0;
}

static int mk_ring_child_thread(void *unused)
{
	struct mk_ring_sender concurrent[2] = {};
	struct mk_ring_sender full[2] = {};
	u8 *maximum;
	unsigned long deadline;
	u32 sequence;
	u32 sent;
	u32 enospc;
	int ret;

	ret = mk_ring_send(NULL, 0, MK_RING_TEST_TYPE_ZERO, true);
	if (ret)
		goto fail;
	maximum = kmalloc(MK_MAX_DATA_SIZE, GFP_KERNEL);
	if (!maximum) {
		ret = -ENOMEM;
		goto fail;
	}
	for (sequence = 0; sequence < MK_MAX_DATA_SIZE; sequence++)
		maximum[sequence] = mk_ring_pattern(sequence);
	((struct mk_ring_packet *)maximum)->magic = MK_RING_TEST_MAGIC;
	((struct mk_ring_packet *)maximum)->phase = MK_RING_PHASE_MAX;
	ret = mk_ring_send(maximum, MK_MAX_DATA_SIZE, MK_RING_TEST_TYPE_DATA, true);
	kfree(maximum);
	if (ret)
		goto fail;
	for (sequence = 0; sequence < MK_RING_FIFO_COUNT; sequence++) {
		ret = mk_ring_send_packet(MK_RING_PHASE_FIFO, 0, sequence,
					  0, 0, true);
		if (ret)
			goto fail;
	}
	ret = mk_ring_run_pair(MK_RING_PHASE_CONCURRENT,
			       MK_RING_CONCURRENT_COUNT, concurrent);
	if (ret)
		goto fail;
	reinit_completion(&full_ack);
	ret = mk_ring_send_packet(MK_RING_PHASE_FULL_BEGIN, 0, 0, 0, 0, true);
	if (ret)
		goto fail;
	if (!wait_for_completion_timeout(&full_ack, msecs_to_jiffies(5000))) {
		ret = -ETIMEDOUT;
		goto fail;
	}
	ret = mk_ring_run_pair(MK_RING_PHASE_FULL_DATA,
			       MK_RING_FULL_LIMIT, full);
	if (ret)
		goto fail;
	sent = full[0].successes + full[1].successes;
	enospc = full[0].enospc + full[1].enospc;
	if (sent != MK_IPI_RING_SIZE - 1 || !enospc) {
		ret = -ENOSPC;
		goto fail;
	}
	msleep(MK_RING_BLOCK_MS + 250);
	ret = mk_ring_send_packet(MK_RING_PHASE_FULL_SUMMARY, 0, 0,
				  sent, enospc, true);
	if (ret)
		goto fail;
	reinit_completion(&fair_stop);
	ret = mk_ring_send_packet(MK_RING_PHASE_FAIR_BEGIN, 0, 0, 0, 0, true);
	if (ret)
		goto fail;
	sequence = 0;
	deadline = jiffies + msecs_to_jiffies(MK_RING_FAIR_TIMEOUT_MS);
	while (!completion_done(&fair_stop) && time_before(jiffies, deadline) &&
	       !kthread_should_stop()) {
		ret = mk_ring_send_fair(sequence);
		if (!ret) {
			sequence++;
			continue;
		}
		if (ret != -ENOSPC)
			goto fail;
		usleep_range(100, 200);
	}
	if (!completion_done(&fair_stop)) {
		ret = -ETIMEDOUT;
		goto fail;
	}
	if (sequence < MK_RING_FAIR_MIN_COUNT) {
		ret = -ENODATA;
		goto fail;
	}
	deadline = jiffies + msecs_to_jiffies(MK_RING_FAIR_TIMEOUT_MS);
	do {
		ret = mk_ring_inject_oversize();
		if (ret == -ENOSPC)
			usleep_range(100, 200);
	} while (ret == -ENOSPC && time_before(jiffies, deadline) &&
		 !kthread_should_stop());
	if (ret)
		goto fail;
	ret = mk_ring_send_packet(MK_RING_PHASE_OVERSIZE_FOLLOWUP, 0, 0,
				  0, 0, true);
	if (ret)
		goto fail;
	WRITE_ONCE(result, 1);
	pr_info("MK_RING_TEST_CHILD_PASS full_sent=%u enospc=%u fairness=%u oversized=1\n",
		sent, enospc, sequence);
	return 0;
fail:
	mk_ring_fail(ret, "child");
	return 0;
}

static int mk_ring_peer_thread(void *unused)
{
	int ret;

	ret = mk_ring_send_packet(MK_RING_PHASE_FAIR_PEER_READY, 0, 0,
				  0, 0, true);
	if (ret) {
		mk_ring_fail(ret, "peer-ready-send");
		return 0;
	}
	if (!wait_for_completion_timeout(&fair_peer_start,
			msecs_to_jiffies(MK_RING_FAIR_TIMEOUT_MS))) {
		mk_ring_fail(-ETIMEDOUT, "peer-start-timeout");
		return 0;
	}
	ret = mk_ring_send_packet(MK_RING_PHASE_FAIR_PEER, 0, 0, 0, 0, true);
	if (ret) {
		mk_ring_fail(ret, "peer-send");
		return 0;
	}
	WRITE_ONCE(result, 1);
	pr_info("MK_RING_TEST_PEER_PASS\n");
	return 0;
}

static int __init mk_ring_test_init(void)
{
	int ret;

	if (!strcmp(role, "host")) {
		zero_handler = multikernel_register_handler(mk_ring_zero_callback,
						    NULL, MK_RING_TEST_TYPE_ZERO);
		data_handler = multikernel_register_handler(mk_ring_data_callback,
						    NULL, MK_RING_TEST_TYPE_DATA);
		fair_handler = multikernel_register_handler(mk_ring_fair_callback,
						    NULL, MK_RING_TEST_TYPE_FAIR);
		dummy_handler = multikernel_register_handler(mk_ring_dummy_callback,
						     NULL, MK_RING_TEST_TYPE_DUMMY);
		if (!zero_handler || !data_handler || !fair_handler || !dummy_handler)
			goto register_failed;
		host_task = kthread_run(mk_ring_host_thread, NULL,
					"mk_ring_host");
		if (IS_ERR(host_task)) {
			ret = PTR_ERR(host_task);
			host_task = NULL;
			goto register_failed_ret;
		}
		pr_info("MK_RING_TEST_HOST_READY target=%d\n", target_id);
		return 0;
	}
	if (strcmp(role, "child") && strcmp(role, "peer"))
		return -EINVAL;
	ret = mk_register_msg_handler(MK_RING_TEST_TYPE_ACK,
				mk_ring_ack_callback, NULL);
	if (ret)
		return ret;
	ack_registered = true;
	if (!strcmp(role, "child")) {
		child_task = kthread_run(mk_ring_child_thread, NULL,
					 "mk_ring_child");
	} else {
		child_task = kthread_run(mk_ring_peer_thread, NULL,
					 "mk_ring_peer");
	}
	if (IS_ERR(child_task)) {
		ret = PTR_ERR(child_task);
		child_task = NULL;
		if (ack_registered) {
			mk_unregister_msg_handler(MK_RING_TEST_TYPE_ACK,
					  mk_ring_ack_callback);
			ack_registered = false;
		}
		return ret;
	}
	return 0;

register_failed:
	ret = -ENOMEM;
register_failed_ret:
	multikernel_unregister_handler(dummy_handler);
	multikernel_unregister_handler(fair_handler);
	multikernel_unregister_handler(data_handler);
	multikernel_unregister_handler(zero_handler);
	fair_handler = NULL;
	data_handler = NULL;
	zero_handler = NULL;
	return ret;
}

static void __exit mk_ring_test_exit(void)
{
	if (child_task)
		kthread_stop(child_task);
	if (host_task)
		kthread_stop(host_task);
	if (ack_registered)
		mk_unregister_msg_handler(MK_RING_TEST_TYPE_ACK,
					  mk_ring_ack_callback);
	multikernel_unregister_handler(dummy_handler);
	multikernel_unregister_handler(fair_handler);
	multikernel_unregister_handler(data_handler);
	multikernel_unregister_handler(zero_handler);
}

module_init(mk_ring_test_init);
module_exit(mk_ring_test_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Multikernel production transport ring fixture");
