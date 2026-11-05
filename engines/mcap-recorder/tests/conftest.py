from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


def decode_protobuf(schema_name: str, schema_data: bytes, data: bytes):
    """Decodes `data` with the protobuf FileDescriptorSet `schema_data`."""
    fds = descriptor_pb2.FileDescriptorSet.FromString(schema_data)
    pool = descriptor_pool.DescriptorPool()
    for f in fds.file:
        pool.Add(f)
    cls = message_factory.GetMessageClass(pool.FindMessageTypeByName(schema_name))
    return cls.FromString(data)


def decode(msg):
    """A foxglove.messages object as a protobuf message, to read its fields."""
    schema = msg.get_schema()
    return decode_protobuf(schema.name, bytes(schema.data), bytes(msg.encode()))


def read_mcap(path):
    """Every message in the MCAP file `path`, in log time order, as
    (topic, schema name, log time, data, message encoding)."""
    from mcap.reader import make_reader

    with open(path, "rb") as f:
        reader = make_reader(f)
        return [
            (
                channel.topic,
                schema.name if schema else "",
                message.log_time,
                message.data,
                channel.message_encoding,
            )
            for schema, channel, message in reader.iter_messages(log_time_order=True)
        ]
